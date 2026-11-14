"""
What a serology photo must be, decided from the file's own bytes.

The type a phone declares for an upload (its Content-Type) and the file name
it sends are both chosen by the sender, so neither is trusted. A file is
accepted only when its first bytes are one of the image formats below, and
it is stored under a random name with the extension of the format it really
is. A web page or script renamed to "photo.jpg", or declared as image/jpeg,
is refused -- so nothing but an image can ever be stored or served back.
"""

import uuid

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # matches the storage bucket's own limit

# ISO base media "ftyp" brands used by HEIC/HEIF photos (iPhones, some Androids).
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}
_HEIF_BRANDS = {b"mif1", b"msf1"}


def detect_image(head):
    """(content_type, extension) for a supported image's first bytes, else None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return "image/heic", "heic"
        if brand in _HEIF_BRANDS:
            return "image/heif", "heif"
    return None


def read_head(fileobj, size=32):
    """The first bytes of an open file, leaving it positioned at the start."""
    fileobj.seek(0)
    head = fileobj.read(size)
    fileobj.seek(0)
    return head


def random_photo_name(extension):
    """A storage name that says nothing about her and can't be guessed."""
    return f"{uuid.uuid4().hex}.{extension}"
