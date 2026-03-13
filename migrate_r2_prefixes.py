"""
Migration script: add language-code prefixes to existing R2 objects.

Old key format: instagram_abc.mp4  /  thumbs/instagram_abc.jpg
New key format: es/instagram_abc.mp4  /  es/thumbs/instagram_abc.jpg

Run with all the same env vars the server uses:
  CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, D1_DATABASE_ID,
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL

  python migrate_r2_prefixes.py [--dry-run]
"""

import os
import sys
import requests
import boto3

DRY_RUN = "--dry-run" in sys.argv

CLOUDFLARE_ACCOUNT_ID = os.environ["CLOUDFLARE_ACCOUNT_ID"]
CLOUDFLARE_API_TOKEN  = os.environ["CLOUDFLARE_API_TOKEN"]
D1_DATABASE_ID        = os.environ["D1_DATABASE_ID"]

R2_ACCESS_KEY_ID      = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY  = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME        = os.environ.get("R2_BUCKET_NAME", "reel-scraper-videos")
R2_PUBLIC_URL         = os.environ["R2_PUBLIC_URL"].rstrip("/")

LANG_TO_CODE = {
    "english": "en",
    "spanish": "es",
    "french":  "fr",
    "other":   "other",
}

def lang_code(language):
    return LANG_TO_CODE.get((language or "other").lower(), "other")


# --- D1 ---
D1_BASE_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
    f"/d1/database/{D1_DATABASE_ID}/query"
)

def d1_query(sql, params=None):
    body = {"sql": sql}
    if params:
        body["params"] = params
    resp = requests.post(
        D1_BASE_URL,
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"D1 query failed: {data.get('errors')}")
    return data["result"][0]


# --- R2 ---
s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)

def r2_key_from_url(url):
    """Strip R2_PUBLIC_URL prefix to get the object key."""
    if url and url.startswith(R2_PUBLIC_URL + "/"):
        return url[len(R2_PUBLIC_URL) + 1:]
    return None

def move_r2_object(old_key, new_key, content_type):
    """Copy old_key → new_key, then delete old_key."""
    print(f"  {'[DRY RUN] ' if DRY_RUN else ''}COPY  {old_key!r} → {new_key!r}")
    if not DRY_RUN:
        s3.copy_object(
            Bucket=R2_BUCKET_NAME,
            CopySource={"Bucket": R2_BUCKET_NAME, "Key": old_key},
            Key=new_key,
            ContentType=content_type,
            MetadataDirective="REPLACE",
        )
        s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)


def main():
    print(f"{'DRY RUN — ' if DRY_RUN else ''}Fetching all rows from D1...")
    result = d1_query("SELECT id, video_url, thumbnail_url, language FROM reels")
    rows = result.get("results", [])
    print(f"Found {len(rows)} rows.\n")

    updated = 0
    skipped = 0
    errors  = 0

    for row in rows:
        row_id    = row["id"]
        video_url = row.get("video_url") or ""
        thumb_url = row.get("thumbnail_url") or ""
        language  = row.get("language") or "other"
        code      = lang_code(language)

        old_video_key = r2_key_from_url(video_url)
        old_thumb_key = r2_key_from_url(thumb_url)

        # Skip rows that already have a language prefix or have no R2 URL
        if not old_video_key:
            skipped += 1
            continue
        if "/" in old_video_key:
            # Already prefixed
            skipped += 1
            continue

        new_video_key = f"{code}/{old_video_key}"
        new_video_url = f"{R2_PUBLIC_URL}/{new_video_key}"

        new_thumb_key = None
        new_thumb_url = thumb_url  # default: unchanged
        if old_thumb_key and "/" not in old_thumb_key.split("thumbs/", 1)[-1]:
            # old: thumbs/foo.jpg  →  new: es/thumbs/foo.jpg
            # (guard: thumbs/ is the only slash, so key is not already prefixed)
            if old_thumb_key.startswith("thumbs/"):
                new_thumb_key = f"{code}/{old_thumb_key}"
            else:
                new_thumb_key = f"{code}/{old_thumb_key}"
            new_thumb_url = f"{R2_PUBLIC_URL}/{new_thumb_key}"

        print(f"Row {row_id} ({language!r} → {code!r}):")
        try:
            move_r2_object(old_video_key, new_video_key, "video/mp4")
            if new_thumb_key and old_thumb_key:
                move_r2_object(old_thumb_key, new_thumb_key, "image/jpeg")

            # Update D1
            print(f"  {'[DRY RUN] ' if DRY_RUN else ''}UPDATE D1 row {row_id}")
            if not DRY_RUN:
                d1_query(
                    "UPDATE reels SET video_url = ?, thumbnail_url = ? WHERE id = ?",
                    [new_video_url, new_thumb_url, row_id],
                )
            updated += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1

    print(f"\nDone. updated={updated}, skipped={skipped}, errors={errors}")


if __name__ == "__main__":
    main()
