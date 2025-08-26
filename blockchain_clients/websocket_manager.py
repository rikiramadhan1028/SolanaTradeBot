# file: blockchain_clients/websocket_manager.py
import json
import asyncio
import logging
from typing import Optional, Callable, Dict, Any
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)

class SolanaWebSocketManager:
    def __init__(self, ws_url: str):
        """
        WebSocket manager for Solana RPC subscriptions
        Args:
            ws_url: WebSocket URL (wss://api.mainnet-beta.solana.com)
        """
        self.ws_url = ws_url
        self.websocket = None
        self.subscription_id = None
        self._running = False
        self._subscription_callbacks: Dict[int, Callable] = {}
        self._next_id = 1

    def _get_ws_url(self, rpc_url: str) -> str:
        """Convert HTTP RPC URL to WebSocket URL"""
        if rpc_url.startswith("https://"):
            return rpc_url.replace("https://", "wss://")
        elif rpc_url.startswith("http://"):
            return rpc_url.replace("http://", "ws://")
        return rpc_url

    async def connect(self) -> bool:
        """Connect to Solana WebSocket"""
        try:
            # Check if already connected
            if self.websocket and hasattr(self.websocket, 'closed') and not self.websocket.closed:
                return True
            elif self.websocket and not hasattr(self.websocket, 'closed'):
                # For older websockets library compatibility
                try:
                    await self.websocket.ping()
                    return True
                except Exception:
                    pass
                
            self.websocket = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self._running = True
            logger.info(f"Connected to Solana WebSocket: {self.ws_url}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket {self.ws_url}: {e}")
            return False

    async def disconnect(self):
        """Disconnect from WebSocket"""
        self._running = False
        if self.websocket:
            try:
                # Check if websocket has closed attribute
                if hasattr(self.websocket, 'closed') and not self.websocket.closed:
                    await self.websocket.close()
                elif not hasattr(self.websocket, 'closed'):
                    # For older websockets library, try to close anyway
                    await self.websocket.close()
            except Exception as e:
                logger.debug(f"Error closing websocket: {e}")
        self.websocket = None
        self._subscription_callbacks.clear()
        logger.info("Disconnected from Solana WebSocket")

    async def subscribe_signature(
        self, 
        signature: str, 
        callback: Callable[[Dict[str, Any]], None],
        commitment: str = "confirmed"
    ) -> Optional[int]:
        """
        Subscribe to signature confirmation updates
        Args:
            signature: Transaction signature to monitor
            callback: Function to call when signature status updates
            commitment: Commitment level (processed, confirmed, finalized)
        Returns:
            Subscription ID or None if failed
        """
        if not await self.connect():
            return None

        subscription_request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "signatureSubscribe",
            "params": [
                signature,
                {
                    "commitment": commitment,
                    "enableReceivedNotification": False
                }
            ]
        }

        try:
            await self.websocket.send(json.dumps(subscription_request))
            
            # Wait for subscription confirmation
            response = await asyncio.wait_for(self.websocket.recv(), timeout=10.0)
            response_data = json.loads(response)
            
            if "result" in response_data:
                sub_id = response_data["result"]
                self._subscription_callbacks[sub_id] = callback
                logger.info(f"Subscribed to signature {signature[:8]}... with ID {sub_id}")
                
                # Start listening task if not already running
                if not hasattr(self, '_listen_task') or self._listen_task.done():
                    self._listen_task = asyncio.create_task(self._listen_loop())
                
                self._next_id += 1
                return sub_id
            else:
                logger.error(f"Subscription failed: {response_data}")
                return None
                
        except Exception as e:
            logger.error(f"Error subscribing to signature {signature}: {e}")
            return None

    async def unsubscribe_signature(self, subscription_id: int) -> bool:
        """Unsubscribe from signature updates"""
        if not self.websocket:
            return False
        if hasattr(self.websocket, 'closed') and self.websocket.closed:
            return False

        unsubscribe_request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "signatureUnsubscribe",
            "params": [subscription_id]
        }

        try:
            await self.websocket.send(json.dumps(unsubscribe_request))
            self._subscription_callbacks.pop(subscription_id, None)
            logger.info(f"Unsubscribed from signature subscription {subscription_id}")
            self._next_id += 1
            return True
        except Exception as e:
            logger.error(f"Error unsubscribing from {subscription_id}: {e}")
            return False

    async def _listen_loop(self):
        """Main listening loop for WebSocket messages"""
        while self._running and self.websocket:
            # Check if websocket is closed (if attribute exists)
            if hasattr(self.websocket, 'closed') and self.websocket.closed:
                break
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=30.0)
                await self._handle_message(message)
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await self.websocket.ping()
                except Exception:
                    break
            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"WebSocket connection lost: {e}")
                break
            except Exception as e:
                logger.error(f"Error in WebSocket listen loop: {e}")
                await asyncio.sleep(1)

        logger.info("WebSocket listen loop ended")

    async def _handle_message(self, message: str):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            
            # Handle subscription notifications
            if "method" in data and data["method"] == "signatureNotification":
                params = data.get("params", {})
                subscription_id = params.get("subscription")
                result = params.get("result")
                
                if subscription_id in self._subscription_callbacks:
                    callback = self._subscription_callbacks[subscription_id]
                    try:
                        callback(result)
                    except Exception as e:
                        logger.error(f"Error in subscription callback: {e}")
                        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode WebSocket message: {e}")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")

    async def wait_for_signature_confirmation(
        self, 
        signature: str, 
        timeout: float = 60.0,
        commitment: str = "confirmed"
    ) -> Dict[str, Any]:
        """
        Wait for signature confirmation with timeout
        Args:
            signature: Transaction signature to wait for
            timeout: Maximum time to wait in seconds
            commitment: Commitment level
        Returns:
            Confirmation result or error info
        """
        result_future = asyncio.Future()
        
        def on_signature_update(notification_result):
            if not result_future.done():
                result_future.set_result(notification_result)
        
        # Subscribe to signature
        sub_id = await self.subscribe_signature(signature, on_signature_update, commitment)
        if not sub_id:
            return {"error": "Failed to subscribe to signature"}
        
        try:
            # Wait for confirmation with timeout
            result = await asyncio.wait_for(result_future, timeout=timeout)
            await self.unsubscribe_signature(sub_id)
            return result
        except asyncio.TimeoutError:
            await self.unsubscribe_signature(sub_id)
            return {"error": f"Signature confirmation timeout after {timeout}s"}
        except Exception as e:
            await self.unsubscribe_signature(sub_id)
            return {"error": f"Error waiting for confirmation: {str(e)}"}

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()