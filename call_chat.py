"""The touchscreen and the native call use the same device-specific Muse chat."""
import os
import uuid


def device_chat_id(node_id, vm_id):
    namespace = os.environ.get("MUSE_CHAT_NAMESPACE", "rdk-x5-muse-chat")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}/{node_id}/{vm_id}"))
