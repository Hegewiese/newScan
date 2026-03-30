"""
Firmware update check functions for Meshtastic devices.
To be integrated into main.py
"""

import re
import json
import logging
from typing import Optional, Tuple, Dict
from datetime import datetime

log = logging.getLogger("newscan")


def parse_firmware_version(version_str: str) -> Tuple[int, int, int, Optional[str]]:
    """
    Parse firmware version string into components.
    
    Args:
        version_str: Version string like "2.2.15.48c8b20", "2.2.15", or "v2.2.15"
        
    Returns:
        Tuple of (major, minor, patch, prerelease)
    """
    if not version_str or version_str == 'N/A':
        return (0, 0, 0, None)
        
    # Clean up version string
    version_str = version_str.strip().lower()
    
    # Remove 'v' prefix and git hash
    version_str = version_str.lstrip('v')
    
    # Split on dots and take first 3 parts
    parts = version_str.split('.')
    
    # Extract major, minor, patch
    major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    
    # Check for prerelease (alpha/beta/rc)
    prerelease = None
    version_lower = version_str.lower()
    if 'alpha' in version_lower:
        prerelease = 'alpha'
    elif 'beta' in version_lower:
        prerelease = 'beta'
    elif 'rc' in version_lower:
        prerelease = 'rc'
    
    return (major, minor, patch, prerelease)


def check_firmware_update(current_version: str, hw_model: str = None) -> Dict:
    """
    Check if firmware needs update.
    Simple offline check - can be enhanced with online API.
    
    Args:
        current_version: Current firmware version string
        hw_model: Hardware model (optional)
        
    Returns:
        Dictionary with update information
    """
    result = {
        'current_version': current_version,
        'update_available': False,
        'is_prerelease': False,
        'message': '',
        'error': None
    }
    
    if not current_version or current_version == 'N/A':
        result['error'] = 'No firmware version available'
        result['message'] = 'Cannot check firmware: No version information'
        return result
    
    # Parse current version
    current_major, current_minor, current_patch, current_prerelease = \
        parse_firmware_version(current_version)
    
    if current_prerelease:
        result['is_prerelease'] = True
    
    # Known latest stable versions (update this periodically)
    # This is a simple offline check - for online check, use GitHub API
    LATEST_STABLE = {
        'major': 2,
        'minor': 2,
        'patch': 15,
        'version': '2.2.15'
    }
    
    # Compare with known latest
    if (current_major, current_minor, current_patch) < \
       (LATEST_STABLE['major'], LATEST_STABLE['minor'], LATEST_STABLE['patch']):
        result['update_available'] = True
        result['message'] = (f"Update available: {current_version} → {LATEST_STABLE['version']}")
    elif (current_major, current_minor, current_patch) > \
         (LATEST_STABLE['major'], LATEST_STABLE['minor'], LATEST_STABLE['patch']):
        # Running newer than known latest (development build)
        result['message'] = (f"Running development build: {current_version} "
                           f"(latest stable is {LATEST_STABLE['version']})")
    else:
        # Same version
        if current_prerelease:
            result['message'] = f"Running prerelease: {current_version}"
        else:
            result['message'] = f"Firmware is up to date: {current_version}"
    
    return result


def format_firmware_message(check_result: Dict, verbose: bool = True) -> str:
    """Format update check result as user-friendly message."""
    if check_result.get('error'):
        return f"⚠  {check_result['error']}"
        
    current = check_result['current_version']
    
    if check_result['update_available']:
        if verbose:
            # Extract version from message
            msg = check_result['message']
            latest = msg.split('→')[-1].strip() if '→' in msg else 'latest'
            return (f"🔴 UPDATE AVAILABLE\n"
                   f"   Current: {current}\n"
                   f"   Latest:  {latest}\n"
                   f"   Visit: https://meshtastic.org/firmware")
        else:
            return f"🔴 Update available"
    else:
        if verbose:
            return f"✅ {check_result['message']}"
        else:
            return f"✅ Up to date"


def quick_firmware_check(iface) -> str:
    """
    Quick firmware check for display after connection.
    Returns a short status message.
    """
    try:
        metadata = iface.metadata
        if not metadata:
            return "⚠  No firmware info"
            
        fw_version = metadata.firmware_version
        if not fw_version:
            return "⚠  No firmware version"
            
        # Perform quick check
        result = check_firmware_update(fw_version)
        return format_firmware_message(result, verbose=False)
        
    except Exception as e:
        log.debug(f"Firmware check error: {e}")
        return "⚠  Check failed"