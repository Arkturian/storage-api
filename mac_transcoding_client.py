"""
Mac Transcoding Client

Client module to submit transcoding jobs to the Mac Transcoding API
via SSH tunnel (localhost:8087 on server).
"""
import os
import httpx
from typing import Optional, Dict, Any
from pathlib import Path
from config import settings  # For API_KEY


class MacTranscodingClient:
    """Client for Mac Transcoding API"""
    
    def __init__(self, api_url: str = None):
        """
        Initialize Mac Transcoding Client
        
        Args:
            api_url: Mac API URL (default: http://localhost:8087 via SSH tunnel)
        """
        self.api_url = api_url or os.getenv("MAC_TRANSCODE_URL", "http://localhost:8087")
        self.timeout = 10.0  # Timeout for API calls (not transcoding itself)
        
    def is_available(self) -> bool:
        """
        Check if Mac transcoding API is available
        
        Returns:
            bool: True if API is reachable and healthy
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{self.api_url}/")
                return response.status_code == 200 and response.json().get("status") == "online"
        except Exception as e:
            print(f"Mac API not available: {e}")
            return False
    
    def submit_job(
        self,
        file_path: str,
        file_size: int,
        filename: str,
        callback_url: str,
        reference_id: str
    ) -> Optional[str]:
        """
        Submit a transcoding job to the Mac API
        
        Args:
            file_path: Absolute path to video file on server (will be converted to download URL)
            file_size: File size in bytes
            filename: Original filename
            callback_url: URL to call when transcoding is complete
            reference_id: Storage object ID for tracking
            
        Returns:
            str: Job ID if successful, None otherwise
        """
        try:
            import uuid
            
            # Generate unique job ID
            job_id = f"job_{reference_id}_{uuid.uuid4().hex[:8]}"
            
            # Convert file path to download URL
            # The Mac API expects a URL it can download from
            # We'll use the storage-api's file download endpoint
            # Get base URL from environment or use current server
            api_base_url = os.getenv("STORAGE_API_BASE_URL", "https://api-storage.arkturian.com")
            source_url = f"{api_base_url}/storage/files/{reference_id}"
            
            payload = {
                "job_id": job_id,
                "source_url": source_url,
                "callback_url": callback_url,
                "file_size_bytes": file_size,
                "original_filename": filename,
                "storage_object_id": reference_id,
                # #1960: the master never leaves storage-api. This client is
                # unused today (only cancel_job is called); a revived path
                # needs a signed /storage/sign URL instead.
                "download_headers": {}
            }
            
            print(f"📤 Submitting transcoding job to Mac API: {filename} ({file_size} bytes)")
            
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.api_url}/transcode",
                    json=payload
                )
                
                if response.status_code == 200:
                    result = response.json()
                    job_id = result.get("job_id")
                    print(f"✅ Mac API accepted job: {job_id}")
                    return job_id
                else:
                    print(f"❌ Mac API rejected job: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            print(f"❌ Failed to submit job to Mac API: {e}")
            return None
    
    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get status of a transcoding job
        
        Args:
            job_id: Job ID to check
            
        Returns:
            dict: Job status information, or None if failed
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{self.api_url}/status/{job_id}")
                
                if response.status_code == 200:
                    return response.json()
                else:
                    return None
                    
        except Exception as e:
            print(f"❌ Failed to get job status: {e}")
            return None
    
    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a transcoding job
        
        Args:
            job_id: Job ID to cancel
            
        Returns:
            bool: True if cancelled successfully
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.api_url}/cancel/{job_id}")
                return response.status_code == 200
                
        except Exception as e:
            print(f"❌ Failed to cancel job: {e}")
            return False


# Global singleton instance
mac_transcoding_client = MacTranscodingClient()

