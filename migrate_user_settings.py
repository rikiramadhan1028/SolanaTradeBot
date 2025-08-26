#!/usr/bin/env python3
# migrate_user_settings.py - Migrate user settings from JSON to MongoDB

import json
import os
import sys
from typing import Dict, Any

def load_json_settings(file_path: str) -> Dict[str, Dict[str, Any]]:
    """Load existing JSON user settings."""
    if not os.path.exists(file_path):
        print(f"JSON settings file not found: {file_path}")
        return {}
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Error loading JSON settings: {e}")
        return {}

def migrate_to_mongodb(json_data: Dict[str, Dict[str, Any]]) -> bool:
    """Migrate JSON data to MongoDB."""
    try:
        # Import database functions
        from database import user_settings_set_cu_price, user_settings_set_priority_tier
        
        migrated_count = 0
        for user_id_str, settings in json_data.items():
            try:
                user_id = int(user_id_str)
                cu_price = settings.get('cu_price')
                priority_tier = settings.get('priority_tier')
                
                # Migrate CU price
                if cu_price is not None:
                    user_settings_set_cu_price(user_id, cu_price)
                
                # Migrate priority tier
                if priority_tier is not None:
                    user_settings_set_priority_tier(user_id, priority_tier)
                
                migrated_count += 1
                print(f"✅ Migrated user {user_id}: CU={cu_price}, Tier={priority_tier}")
                
            except Exception as e:
                print(f"❌ Error migrating user {user_id_str}: {e}")
                continue
        
        print(f"\n🎉 Migration completed: {migrated_count}/{len(json_data)} users migrated")
        return True
        
    except ImportError:
        print("❌ Error: Cannot import database functions. Check MongoDB connection.")
        return False
    except Exception as e:
        print(f"❌ Migration error: {e}")
        return False

def backup_json_file(file_path: str) -> bool:
    """Create backup of JSON file."""
    backup_path = file_path + ".migrated_backup"
    try:
        import shutil
        shutil.copy2(file_path, backup_path)
        print(f"✅ Created backup: {backup_path}")
        return True
    except Exception as e:
        print(f"❌ Error creating backup: {e}")
        return False

def main():
    print("🔄 User Settings Migration Tool")
    print("=" * 40)
    
    # Path to JSON settings file
    json_file = os.path.join(os.path.dirname(__file__), "user_settings.json")
    
    # Load JSON data
    print(f"📂 Loading JSON settings from: {json_file}")
    json_data = load_json_settings(json_file)
    
    if not json_data:
        print("ℹ️  No user settings found to migrate.")
        return
    
    print(f"📊 Found {len(json_data)} users to migrate:")
    for user_id, settings in json_data.items():
        cu_price = settings.get('cu_price', 'None')
        tier = settings.get('priority_tier', 'None')
        print(f"  • User {user_id}: CU={cu_price}, Tier={tier}")
    
    # Confirm migration
    print(f"\n❓ Proceed with migration to MongoDB? (y/N): ", end="")
    response = input().strip().lower()
    
    if response != 'y':
        print("🚫 Migration cancelled.")
        return
    
    # Create backup first
    print(f"\n💾 Creating backup...")
    if not backup_json_file(json_file):
        print("❌ Cannot proceed without backup.")
        return
    
    # Perform migration
    print(f"\n🔄 Migrating to MongoDB...")
    success = migrate_to_mongodb(json_data)
    
    if success:
        print(f"\n✅ Migration successful!")
        print(f"💡 You can now safely remove the JSON file: {json_file}")
        print(f"📄 Backup is available at: {json_file}.migrated_backup")
    else:
        print(f"\n❌ Migration failed!")
        print(f"📄 Your original data is safe in: {json_file}")

if __name__ == "__main__":
    main()