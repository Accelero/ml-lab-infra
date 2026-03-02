"""Custom Pulumi resources."""

from resources.ts_device_cleanup import TailscaleDeviceCleanup

__all__ = [
    "TailscaleDeviceCleanup",
]
