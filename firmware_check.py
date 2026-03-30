#!/usr/bin/env python3
"""
Firmware version checker for Meshtastic devices.
Checks if connected device firmware is up to date.
"""

import re
import requests
import json
import logging
from typing import Optional, Tuple, Dict
from datetime import datetime

log = logging.getLogger(__name__)


class FirmwareChecker:
    """Check if Meshtastic firmware is up to date."""
    
    # GitHub API URL for Meshtastic firmware releases
    GITHUB_API_URL = "https://api.github.com/repos/meshtastic/firmware/releases"
    
    # Cache for release data to avoid multiple API calls
    _release_cache = None
    _cache_timestamp = None
    CACHE_TIMEOUT = 3600  # 1 hour
    
    @classmethod
    def _get_latest_releases(cls) -> Optional[Dict]:
        """Get latest firmware releases from GitHub API."""
        # Check cache
        if cls._release_cache and cls._cache_timestamp:
            age = datetime.now().timestamp() - cls._cache_timestamp
            if age < cls.CACHE_TIMEOUT:
                return cls._release_cache
        
        try:
            log.debug("Fetching latest firmware releases from GitHub...")
            response = requests.get(cls.GITHUB_API_URL, timeout=10)
            response.raise_for_status()
            releases = response.json()
            
            # Cache the result
            cls._release_cache = releases
            cls._cache_timestamp = datetime.now().timestamp()
            
            return releases
            
        except requests.exceptions.RequestException as e:
            log.warning(f"Failed to fetch firmware releases: {e}")
            return None
        except json.JSONDecodeError as e:
            log.warning(f"Failed to parse firmware releases: {e}")
            return None
    
    @staticmethod
    def parse_firmware_version(version_str: str) -> Tuple[int, int, int, Optional[str]]:
        """
        Parse firmware version string into components.
        
        Args:
            version_str: Version string like "2.2.15.48c8b20", "2.2.15", or "v2.2.15"
            
        Returns:
            Tuple of (major, minor, patch, prerelease)
        """
        if not version_str:
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
    
    @classmethod
    def get_latest_stable_version(cls) -> Optional[str]:
        """Get the latest stable firmware version."""
        releases = cls._get_latest_releases()
        if not releases:
            return None
            
        # Find latest stable release (not prerelease)
        for release in releases:
            if not release.get('prerelease', False):
                tag_name = release.get('tag_name', '')
                if tag_name:
                    return tag_name.lstrip('v')
        
        return None
    
    @classmethod
    def get_latest_prerelease_version(cls) -> Optional[str]:
        """Get the latest prerelease firmware version."""
        releases = cls._get_latest_releases()
        if not releases:
            return None
            
        # Find latest prerelease
        for release in releases:
            if release.get('prerelease', False):
                tag_name = release.get('tag_name', '')
                if tag_name:
                    return tag_name.lstrip('v')
        
        return None
    
    @classmethod
    def check_firmware_update(cls, current_version: str, hw_model: str = None) -> Dict:
        """
        Check if firmware needs update.
        
        Args:
            current_version: Current firmware version string
            hw_model: Hardware model (optional, for future use)
            
        Returns:
            Dictionary with update information
        """
        result = {
            'current_version': current_version,
            'latest_version': None,
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
            cls.parse_firmware_version(current_version)
        
        # Get latest releases
        latest_stable = cls.get_latest_stable_version()
        latest_prerelease = cls.get_latest_prerelease_version()
        
        if not latest_stable and not latest_prerelease:
            result['error'] = 'Could not fetch latest versions'
            result['message'] = 'Cannot check for updates: Network error'
            return result
        
        # Determine which version to compare against
        compare_version = None
        is_prerelease = False
        
        if current_prerelease:
            # If running prerelease, compare against latest prerelease
            compare_version = latest_prerelease
            is_prerelease = True
        else:
            # If running stable, compare against latest stable
            compare_version = latest_stable
            
        if not compare_version:
            compare_version = latest_stable or latest_prerelease
            
        result['latest_version'] = compare_version
        result['is_prerelease'] = is_prerelease
        
        # Parse latest version
        latest_major, latest_minor, latest_patch, latest_prerelease = \
            cls.parse_firmware_version(compare_version)
        
        # Compare versions
        if (current_major, current_minor, current_patch) < (latest_major, latest_minor, latest_patch):
            result['update_available'] = True
            
            if current_prerelease and not latest_prerelease:
                # Moving from prerelease to stable
                result['message'] = (f"Update available: {current_version} → {compare_version} "
                                   f"(prerelease to stable)")
            elif not current_prerelease and latest_prerelease:
                # Stable to prerelease (optional)
                result['message'] = (f"Prerelease available: {current_version} → {compare_version}")
            else:
                # Same type (stable→stable or prerelease→prerelease)
                result['message'] = f"Update available: {current_version} → {compare_version}"
                
        elif (current_major, current_minor, current_patch) > (latest_major, latest_minor, latest_patch):
            # Running newer than latest (development build)
            result['message'] = (f"Running development build: {current_version} "
                               f"(latest is {compare_version})")
        else:
            # Same version
            if current_prerelease:
                result['message'] = f"Running latest prerelease: {current_version}"
            else:
                result['message'] = f"Firmware is up to date: {current_version}"
        
        return result
    
    @classmethod
    def format_update_message(cls, check_result: Dict, verbose: bool = True) -> str:
        """Format update check result as user-friendly message."""
        if check_result.get('error'):
            return f"⚠  {check_result['error']}"
            
        current = check_result['current_version']
        latest = check_result['latest_version']
        
        if not latest:
            return f"Current: {current} (cannot check for updates)"
            
        if check_result['update_available']:
            if verbose:
                return (f"🔴 UPDATE AVAILABLE\n"
                       f"   Current: {current}\n"
                       f"   Latest:  {latest}\n"
                       f"   Visit: https://meshtastic.org/firmware")
            else:
                return f"🔴 Update: {current} → {latest}"
        else:
            if verbose:
                return f"✅ Up to date: {current}"
            else:
                return f"✅ {current}"


def check_device_firmware(iface) -> Dict:
    """
    Check firmware version of connected Meshtastic device.
    
    Args:
        iface: Connected Meshtastic interface (BLE or serial)
        
    Returns:
        Dictionary with firmware check results
    """
    try:
        # Get firmware version from device
        metadata = iface.metadata
        if not metadata:
            return {
                'error': 'No metadata available',
                'message': 'Cannot read device metadata'
            }
            
        fw_version = metadata.firmware_version
        hw_model = metadata.hw_model if hasattr(metadata, 'hw_model') else None
        
        if not fw_version:
            return {
                'error': 'No firmware version',
                'message': 'Device firmware version not available'
            }
            
        # Check for updates
        return FirmwareChecker.check_firmware_update(fw_version, hw_model)
        
    except Exception as e:
        return {
            'error': f'Check failed: {str(e)}',
            'message': f'Firmware check error: {str(e)}'
        }


if __name__ == "__main__":
    # Test the firmware checker
    import logging
    logging.basicConfig(level=logging.INFO)
    
    print("Testing firmware checker...")
    
    # Test cases
    test_versions = [
        "2.2.15.48c8b20",
        "2.2.14",
        "v2.2.15",
        "2.3.0.alpha1",
        "N/A",
        "",
    ]
    
    for version in test_versions:
        print(f"\nChecking version: {version}")
        result = FirmwareChecker.check_firmware_update(version)
        message = FirmwareChecker.format_update_message(result, verbose=True)
        print(message)