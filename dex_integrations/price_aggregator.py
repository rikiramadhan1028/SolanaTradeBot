# dex_integrations/price_aggregator.py
import httpx
import json

# Endpoint API harga publik dari Raydium
RAYDIUM_PRICE_API_URL = "https://api-v3.raydium.io/price/all"

# Endpoint API harga dari PumpPortal (data API)
PUMPPORTAL_DATA_API_URL = "https://pumpportal.fun/api/data"

async def get_token_price_from_raydium(token_address: str) -> dict:
    """
    Mengambil harga real-time, market cap, dan info likuiditas dari API Raydium.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(RAYDIUM_PRICE_API_URL)
            response.raise_for_status()
            price_data = response.json()
            
            # Mencari harga token berdasarkan alamat kontrak
            token_info = price_data.get(token_address)
            
            if token_info:
                return {
                    "price": token_info.get("price"),
                    "lp": "N/A",
                    "mc": token_info.get("marketCap")
                }
            else:
                return {"price": 0, "lp": "N/A", "mc": "N/A"}
    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        return {"price": 0, "lp": "N/A", "mc": "N/A"}
    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}.")
        return {"price": 0, "lp": "N/A", "mc": "N/A"}
    except Exception as e:
        print(f"Error fetching token price: {e}")
        return {"price": 0, "lp": "N/A", "mc": "N/A"}

async def get_token_price_from_pumpfun(token_address: str) -> dict:
    """
    Mengambil data harga dari API data PumpPortal.
    """
    # Catatan: API data PumpPortal menggunakan WebSocket,
    # tetapi untuk kesederhanaan, kita bisa menggunakan API REST pihak ketiga jika tersedia.
    # Namun, karena tidak ada API REST publik yang jelas untuk ini, kita akan menggunakan
    # data dari API trading sebagai fallback untuk mendapatkan info dasar.
    # Implementasi ini mengasumsikan API quote dari Jupiter atau API lain yang serupa.
    
    # Placeholder untuk API quote Pumpfun yang spesifik.
    # Kita bisa menggunakan endpoint quote dari Jupiter yang mendukung Pumpfun.
    QUOTE_API_URL = "https://public.jupiterapi.com/pump-fun/quote"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                QUOTE_API_URL,
                params={
                    "mint": token_address,
                    "type": "BUY",
                    "amount": 1_000_000_000 # Contoh: 1 SOL
                }
            )
            response.raise_for_status()
            quote_data = response.json()
            
            if quote_data:
                # API ini memberikan currentMarketCapInSol, yang bisa kita gunakan
                price = float(quote_data.get("currentMarketCapInSol")) / float(quote_data.get("totalSupply"))
                return {
                    "price": price,
                    "lp": quote_data.get("liquidity", "N/A"),
                    "mc": quote_data.get("currentMarketCapInSol")
                }
            else:
                return {"price": 0, "lp": "N/A", "mc": "N/A"}

    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        return {"price": 0, "lp": "N/A", "mc": "N/A"}
    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}.")
        return {"price": 0, "lp": "N/A", "mc": "N/A"}
    except Exception as e:
        print(f"Error fetching token price from Pumpfun: {e}")
        return {"price": 0, "lp": "N/A", "mc": "N/A"}