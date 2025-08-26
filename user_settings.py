# user_settings.py - Persistent user settings storage (MongoDB backend)
import os
from typing import Optional, Dict, Any
import logging

# Import database functions
try:
    from database import (
        user_settings_get, user_settings_set_cu_price, user_settings_set_priority_tier,
        user_settings_get_cu_price, user_settings_get_priority_tier,
        user_settings_remove, user_settings_list_all, user_settings_count
    )
    MONGODB_AVAILABLE = True
except ImportError:
    MONGODB_AVAILABLE = False

logger = logging.getLogger(__name__)

class UserSettings:
    """Manages persistent user settings with MongoDB backend."""
    
    @staticmethod
    def load_all_settings() -> Dict[str, Dict[str, Any]]:
        """Load all user settings from MongoDB."""
        if not MONGODB_AVAILABLE:
            logger.warning("MongoDB not available, returning empty settings")
            return {}
            
        try:
            docs = user_settings_list_all()
            result = {}
            for doc in docs:
                user_id = str(doc.get('user_id', ''))
                if user_id:
                    result[user_id] = {
                        'cu_price': doc.get('cu_price'),
                        'priority_tier': doc.get('priority_tier'),
                        'updated_at': doc.get('updated_at')
                    }
            logger.info(f"Loaded settings for {len(result)} users from MongoDB")
            return result
        except Exception as e:
            logger.error(f"Error loading user settings from MongoDB: {e}")
            return {}
    
    @staticmethod
    def get_user_setting(user_id: str, key: str, default: Any = None) -> Any:
        """Get a specific setting for a user."""
        if not MONGODB_AVAILABLE:
            return default
            
        try:
            doc = user_settings_get(int(user_id))
            return doc.get(key, default)
        except Exception as e:
            logger.error(f"Error getting user setting {key} for {user_id}: {e}")
            return default
    
    @staticmethod
    def set_user_setting(user_id: str, key: str, value: Any) -> bool:
        """Set a specific setting for a user."""
        if not MONGODB_AVAILABLE:
            logger.warning("MongoDB not available, cannot save user setting")
            return False
            
        try:
            user_id_int = int(user_id)
            
            # Handle specific known keys with dedicated functions
            if key == 'cu_price':
                user_settings_set_cu_price(user_id_int, value)
            elif key == 'priority_tier':
                user_settings_set_priority_tier(user_id_int, value)
            else:
                # For other keys, we'd need a more generic update method
                logger.warning(f"Unknown setting key: {key}")
                return False
                
            logger.info(f"Updated setting {key}={value} for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting user setting {key}={value} for {user_id}: {e}")
            return False
    
    @staticmethod
    def get_user_cu_price(user_id: str, default: Optional[int] = None) -> Optional[int]:
        """Get user's preferred CU price setting."""
        if not MONGODB_AVAILABLE:
            return default
            
        try:
            return user_settings_get_cu_price(int(user_id)) or default
        except Exception as e:
            logger.error(f"Error getting CU price for user {user_id}: {e}")
            return default
    
    @staticmethod
    def set_user_cu_price(user_id: str, cu_price: Optional[int]) -> bool:
        """Set user's preferred CU price setting."""
        if not MONGODB_AVAILABLE:
            logger.warning("MongoDB not available, cannot save CU price")
            return False
            
        try:
            user_settings_set_cu_price(int(user_id), cu_price)
            logger.info(f"Updated CU price to {cu_price} for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting CU price for user {user_id}: {e}")
            return False
    
    @staticmethod
    def get_user_priority_tier(user_id: str, default: Optional[str] = None) -> Optional[str]:
        """Get user's preferred priority tier (for reference)."""
        if not MONGODB_AVAILABLE:
            return default
            
        try:
            return user_settings_get_priority_tier(int(user_id)) or default
        except Exception as e:
            logger.error(f"Error getting priority tier for user {user_id}: {e}")
            return default
    
    @staticmethod
    def set_user_priority_tier(user_id: str, tier: Optional[str]) -> bool:
        """Set user's preferred priority tier (for reference)."""
        if not MONGODB_AVAILABLE:
            logger.warning("MongoDB not available, cannot save priority tier")
            return False
            
        try:
            user_settings_set_priority_tier(int(user_id), tier)
            logger.info(f"Updated priority tier to {tier} for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error setting priority tier for user {user_id}: {e}")
            return False
    
    @staticmethod
    def remove_user(user_id: str) -> bool:
        """Remove all settings for a user."""
        if not MONGODB_AVAILABLE:
            logger.warning("MongoDB not available, cannot remove user settings")
            return False
            
        try:
            user_settings_remove(int(user_id))
            logger.info(f"Removed all settings for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error removing settings for user {user_id}: {e}")
            return False
    
    @staticmethod
    def get_all_users() -> list:
        """Get list of all user IDs with settings."""
        if not MONGODB_AVAILABLE:
            return []
            
        try:
            docs = user_settings_list_all()
            return [str(doc.get('user_id')) for doc in docs if doc.get('user_id')]
        except Exception as e:
            logger.error(f"Error getting all users: {e}")
            return []
    
    @staticmethod
    def get_user_settings_summary(user_id: str) -> Dict[str, Any]:
        """Get summary of all settings for a user."""
        if not MONGODB_AVAILABLE:
            return {}
            
        try:
            doc = user_settings_get(int(user_id))
            return {
                'cu_price': doc.get('cu_price'),
                'priority_tier': doc.get('priority_tier'),
                'updated_at': doc.get('updated_at')
            }
        except Exception as e:
            logger.error(f"Error getting settings summary for user {user_id}: {e}")
            return {}

# Helper functions for backward compatibility
def get_user_cu_price(user_id: str, default: Optional[int] = None) -> Optional[int]:
    """Helper function to get user CU price."""
    return UserSettings.get_user_cu_price(user_id, default)

def set_user_cu_price(user_id: str, cu_price: Optional[int]) -> bool:
    """Helper function to set user CU price."""
    return UserSettings.set_user_cu_price(user_id, cu_price)

# Initialize settings on module import (MongoDB doesn't need explicit loading)
if MONGODB_AVAILABLE:
    logger.info("User settings initialized with MongoDB backend")
else:
    logger.warning("MongoDB not available, user settings will not persist")