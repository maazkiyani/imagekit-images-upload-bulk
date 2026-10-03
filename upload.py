import csv
import io
import mimetypes
import os
import posixpath
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st


# ============================================================
# ImageKit Bulk Uploader - Single-file Streamlit App
# Supports direct image uploads OR ZIP archives
# ============================================================

UPLOAD_URL = "https://upload.imagekit.io/api/v1/files/upload"

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff"
}
SUPPORTED_TYPES = [ext.lstrip(".") for ext in sorted(SUPPORTED_EXTENSIONS)]


def normalize_folder(folder: str) -> str:
    folder = (folder or "").strip().replace("\\", "/")
    if not folder:
        return "/pinterest"
    if not folder.startswith("/"):
        folder = "/" + folder
    while "//" in folder:
        folder = folder.replace("//", "/")
    if len(folder) > 1 and folder.endswith("/"):
        folder = folder[:-1]
    return folder


def join_imagekit_folder(base_folder: str, relative_parent: str) -> str:
    base_folder = normalize_folder(base_folder)
    relative_parent = (relative_parent or "").replace("\\", "/").strip("/")
    if not relative_parent or relative_parent == ".":
        return base_folder
    return normalize_folder(base_folder + "/" + relative_parent)


def safe_zip_member_path(name: str):
    """
    Return a safe normalized ZIP member path, or None if unsafe.
    Prevents absolute paths and ../ traversal.
    """
    name = (name or "").replace("\\", "/")
    normalized = posixpath.normpath(name)

    if normalized in ("", "."):
        return None

    if normalized.startswith("../") or normalized == "..":
        return None

    if normalized.startswith("/"):
        return None

    # Ignore common metadata folders
    parts = normalized.split("/")
    if any(part == "__MACOSX" for part in parts):
        return None

    return normalized


def prepare_direct_images(uploaded_files):
    items = []

    for uploaded_file in uploaded_files or []:
        filename = uploaded_file.name
        ext = os.path.splitext(filename)[1].lower()

        if ext not in SUPPORTED_EXTENSIONS:
            continue

        mime_type = uploaded_file.type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        items.append({
            "display_name": filename,
            "filename": os.path.basename(filename),
            "relative_parent": "",
            "bytes": uploaded_file.getvalue(),
            "mime_type": mime_type,
            "source": "Direct upload",
        })

    return items


def prepare_zip_images(zip_files, preserve_zip_structure=True):
    """
    Extract supported image files from one or more uploaded ZIP files in memory.
    Nothing is written to disk.
    """
    items = []
    errors = []

    for uploaded_zip in zip_files or []:
        try:
            zip_bytes = uploaded_zip.getvalue()

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue

                    safe_name = safe_zip_member_path(info.filename)
                    if not safe_name:
                        continue

                    ext = os.path.splitext(safe_name)[1].lower()
                    if ext not in SUPPORTED_EXTENSIONS:
                        continue

                    try:
                        file_bytes = zf.read(info)
                    except Exception as exc:
                        errors.append(f"{uploaded_zip.name} → {info.filename}: {exc}")
                        continue

                    filename = posixpath.basename(safe_name)
                    relative_parent = posixpath.dirname(safe_name) if preserve_zip_structure else ""
                    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

                    items.append({
                        "display_name": f"{uploaded_zip.name} → {safe_name}",
                        "filename": filename,
                        "relative_parent": relative_parent,
                        "bytes": file_bytes,
                        "mime_type": mime_type,
                        "source": uploaded_zip.name,
                    })

        except zipfile.BadZipFile:
            errors.append(f"{uploaded_zip.name}: Invalid or corrupted ZIP file.")
        except Exception as exc:
            errors.append(f"{uploaded_zip.name}: {exc}")

    return items, errors


def upload_one(item, private_key, base_folder, preserve_structure, verify_public=True):
    filename = item["filename"]

    relative_parent = item.get("relative_parent", "") if preserve_structure else ""
    imagekit_folder = join_imagekit_folder(base_folder, relative_parent)

    try:
        files = {
            "file": (
                filename,
                item["bytes"],
                item.get("mime_type") or "application/octet-stream",
            )
        }

        data = {
            "fileName": filename,
            "folder": imagekit_folder,
            "useUniqueFileName": "false",
            "overwriteFile": "true",
            "isPrivateFile": "false",
        }

        response = requests.post(
            UPLOAD_URL,
            auth=(private_key, ""),
            files=files,
            data=data,
            timeout=(30, 180),
        )

        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text[:500]

            return {
                "Original Filename": filename,
                "Source": item.get("source", ""),
                "Relative Folder": relative_parent,
                "ImageKit Folder": imagekit_folder,
                "ImageKit URL": "",
                "File ID": "",
                "Status": "FAILED",
                "Public Check": "",
                "Error": f"HTTP {response.status_code}: {detail}",
            }

        payload = response.json()
        image_url = payload.get("url", "")
        file_id = payload.get("fileId", "")

        if not image_url:
            return {
                "Original Filename": filename,
                "Source": item.get("source", ""),
                "Relative Folder": relative_parent,
                "ImageKit Folder": imagekit_folder,
                "ImageKit URL": "",
                "File ID": file_id,
                "Status": "FAILED",
                "Public Check": "",
                "Error": "ImageKit upload completed but no URL was returned.",
            }

        public_check = "Not checked"

        if verify_public:
            try:
                check = requests.get(
                    image_url,
                    stream=True,
                    allow_redirects=True,
                    timeout=(15, 30),
                    headers={"User-Agent": "Mozilla/5.0 Pinterest-Media-Check"},
                )

                content_type = (check.headers.get("Content-Type") or "").split(";")[0].lower()

                if check.status_code == 200 and content_type.startswith("image/"):
                    public_check = f"OK - {check.status_code} {content_type}"
                else:
                    public_check = f"CHECK - {check.status_code} {content_type or 'unknown'}"

                check.close()

            except Exception as exc:
                public_check = f"CHECK - {exc}"

        return {
            "Original Filename": filename,
            "Source": item.get("source", ""),
            "Relative Folder": relative_parent,
            "ImageKit Folder": imagekit_folder,
            "ImageKit URL": image_url,
            "File ID": file_id,
            "Status": "SUCCESS",
            "Public Check": public_check,
            "Error": "",
        }

    except Exception as exc:
        return {
            "Original Filename": filename,
            "Source": item.get("source", ""),
            "Relative Folder": relative_parent,
            "ImageKit Folder": imagekit_folder,
            "ImageKit URL": "",
            "File ID": "",
            "Status": "FAILED",
            "Public Check": "",
            "Error": str(exc),
        }


def results_to_csv(results):
    output = io.StringIO()

    fieldnames = [
        "Original Filename",
        "Source",
        "Relative Folder",
        "ImageKit Folder",
        "ImageKit URL",
        "File ID",
        "Status",
        "Public Check",
        "Error",
    ]

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(results)

    return output.getvalue().encode("utf-8-sig")


def successful_urls_to_txt(results):
    urls = [
        row["ImageKit URL"]
        for row in results
        if row["Status"] == "SUCCESS" and row["ImageKit URL"]
    ]

    return ("\n".join(urls) + ("\n" if urls else "")).encode("utf-8")


def failed_to_csv(results):
    failed = [row for row in results if row["Status"] != "SUCCESS"]

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Original Filename", "Source", "Error"])

    for row in failed:
        writer.writerow([
            row["Original Filename"],
            row["Source"],
            row["Error"],
        ])

    return output.getvalue().encode("utf-8-sig")


def find_duplicate_targets(items, base_folder, preserve_structure):
    """
    Warn when multiple selected files would upload to the exact same
    ImageKit folder + filename, because overwriteFile=True would replace them.
    """
    seen = {}
    duplicates = []

    for item in items:
        relative_parent = item.get("relative_parent", "") if preserve_structure else ""
        folder = join_imagekit_folder(base_folder, relative_parent)
        key = (folder.lower(), item["filename"].lower())

        if key in seen:
            duplicates.append((seen[key], item["display_name"], folder, item["filename"]))
        else:
            seen[key] = item["display_name"]

    return duplicates


# ============================================================
# Streamlit UI
# ============================================================

st.set_page_config(
    page_title="ImageKit Bulk Uploader",
    page_icon="🖼️",
    layout="wide",
)

st.title("🖼️ ImageKit Bulk Uploader")
st.caption(
    "Upload direct images or ZIP archives, send all images to ImageKit, "
    "and export Pinterest-ready direct media URLs."
)

if "upload_results" not in st.session_state:
    st.session_state.upload_results = []


# ----------------------------
# Sidebar settings
# ----------------------------

with st.sidebar:
    st.header("ImageKit Settings")

    private_key = st.text_input(
        "Private API Key",
        value=os.environ.get("IMAGEKIT_PRIVATE_KEY", ""),
        type="password",
        help="ImageKit Dashboard → Developer options → API keys → Private key",
    )

    destination_folder = normalize_folder(
        st.text_input(
            "ImageKit base folder",
            value="/pinterest",
            help="Example: /pinterest/september",
        )
    )

    workers = st.slider(
        "Parallel uploads",
        min_value=1,
        max_value=8,
        value=4,
        help="Use 2–4 if your internet connection is unstable.",
    )

    verify_public = st.checkbox(
        "Verify returned URLs",
        value=True,
        help="Checks each ImageKit URL for HTTP 200 and an image Content-Type.",
    )

    st.divider()

    st.warning(
        "Keep your Private API Key secret. Never publish it in GitHub, "
        "JavaScript, a public website, or screenshots."
    )


# ----------------------------
# Upload mode
# ----------------------------

st.subheader("1. Choose upload method")

upload_mode = st.radio(
    "How do you want to provide the images?",
    ["📁 Direct images", "🗜️ ZIP file"],
    horizontal=True,
)

items = []
prep_errors = []
preserve_structure = False


if upload_mode == "📁 Direct images":
    uploaded_files = st.file_uploader(
        "Choose images",
        type=SUPPORTED_TYPES,
        accept_multiple_files=True,
        help="You can select hundreds of images at once with Ctrl+A.",
        key="direct_images",
    )

    items = prepare_direct_images(uploaded_files)

    if items:
        total_size = sum(len(item["bytes"]) for item in items)

        st.success(
            f"Selected **{len(items)} images** "
            f"({total_size / 1024 / 1024:.1f} MB total)."
        )

        with st.expander("Preview selected files"):
            for i, item in enumerate(items, start=1):
                st.write(f"{i}. {item['filename']}")


else:
    zip_files = st.file_uploader(
        "Choose ZIP file(s)",
        type=["zip"],
        accept_multiple_files=True,
        help="The app extracts supported images from the ZIP in memory. It does not modify your ZIP.",
        key="zip_files",
    )

    preserve_structure = st.checkbox(
        "Preserve folders inside ZIP in ImageKit",
        value=True,
        help=(
            "Example: ZIP/Recipes/Soup/image.jpg → "
            "/pinterest/Recipes/Soup/image.jpg"
        ),
    )

    items, prep_errors = prepare_zip_images(
        zip_files,
        preserve_zip_structure=preserve_structure,
    )

    if zip_files:
        st.info(f"ZIP files selected: **{len(zip_files)}**")

    if items:
        total_size = sum(len(item["bytes"]) for item in items)

        st.success(
            f"Found **{len(items)} supported images** inside the ZIP file(s) "
            f"({total_size / 1024 / 1024:.1f} MB extracted)."
        )

        with st.expander("Preview images found inside ZIP"):
            for i, item in enumerate(items, start=1):
                folder_text = item["relative_parent"] or "/"
                st.write(
                    f"{i}. {item['filename']}  —  folder: `{folder_text}`"
                )

    if prep_errors:
        st.warning("Some ZIP entries could not be read:")
        for error in prep_errors[:20]:
            st.write(f"- {error}")

        if len(prep_errors) > 20:
            st.write(f"...and {len(prep_errors) - 20} more.")


# ----------------------------
# Duplicate target warning
# ----------------------------

duplicates = find_duplicate_targets(
    items,
    destination_folder,
    preserve_structure=preserve_structure,
) if items else []

if duplicates:
    st.error(
        f"⚠️ Found {len(duplicates)} duplicate ImageKit destination filename(s). "
        "Because overwrite is enabled, one file could replace another."
    )

    with st.expander("Show duplicate destinations"):
        for first, second, folder, filename in duplicates[:50]:
            st.write(
                f"`{folder}/{filename}`\n\n"
                f"- First: {first}\n"
                f"- Duplicate: {second}"
            )


# ----------------------------
# Start upload
# ----------------------------

st.subheader("2. Upload to ImageKit")

col1, col2 = st.columns([1, 4])

with col1:
    start_upload = st.button(
        "🚀 START UPLOAD",
        type="primary",
        use_container_width=True,
        disabled=not bool(items),
    )

with col2:
    if st.button("Clear previous results"):
        st.session_state.upload_results = []
        st.rerun()


if start_upload:
    if not private_key.strip():
        st.error("Paste your ImageKit Private API Key first.")
        st.stop()

    if not items:
        st.error("Choose images or a ZIP file first.")
        st.stop()

    if duplicates:
        st.error(
            "Fix the duplicate destination filenames before uploading. "
            "You can preserve ZIP folders or rename duplicate files."
        )
        st.stop()

    st.session_state.upload_results = []

    progress = st.progress(0)
    status = st.empty()
    live_table = st.empty()

    results = []
    total = len(items)

    # Preserve exact input order for final output.
    order = {id(item): i for i, item in enumerate(items)}
    indexed_items = list(enumerate(items))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                upload_one,
                item,
                private_key.strip(),
                destination_folder,
                preserve_structure,
                verify_public,
            ): index
            for index, item in indexed_items
        }

        completed = 0
        indexed_results = []

        for future in as_completed(futures):
            index = futures[future]
            result = future.result()
            indexed_results.append((index, result))

            completed += 1
            progress.progress(completed / total)

            current_results = [r for _, r in indexed_results]
            success_count = sum(r["Status"] == "SUCCESS" for r in current_results)
            fail_count = completed - success_count

            status.write(
                f"Processed **{completed}/{total}** — "
                f"✅ {success_count} successful | ❌ {fail_count} failed"
            )

            live_rows = [
                {
                    "Filename": r["Original Filename"],
                    "Status": r["Status"],
                    "URL": r["ImageKit URL"],
                }
                for _, r in indexed_results[-12:]
            ]

            live_table.dataframe(
                live_rows,
                use_container_width=True,
                hide_index=True,
            )

    indexed_results.sort(key=lambda x: x[0])
    results = [result for _, result in indexed_results]

    st.session_state.upload_results = results

    progress.progress(1.0)
    status.success("Upload batch finished.")


# ----------------------------
# Results / downloads
# ----------------------------

results = st.session_state.upload_results

if results:
    successful = [r for r in results if r["Status"] == "SUCCESS"]
    failed = [r for r in results if r["Status"] != "SUCCESS"]

    st.divider()
    st.subheader("3. Results")

    a, b, c = st.columns(3)

    a.metric("Total", len(results))
    b.metric("Successful", len(successful))
    c.metric("Failed", len(failed))

    st.dataframe(
        results,
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Download output")

    d1, d2, d3 = st.columns(3)

    with d1:
        st.download_button(
            "⬇️ Filename → URL CSV",
            data=results_to_csv(results),
            file_name="imagekit_links.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with d2:
        st.download_button(
            "⬇️ Direct URL list TXT",
            data=successful_urls_to_txt(results),
            file_name="imagekit_links.txt",
            mime="text/plain",
            use_container_width=True,
        )

    with d3:
        st.download_button(
            "⬇️ Failed uploads CSV",
            data=failed_to_csv(results),
            file_name="failed_uploads.csv",
            mime="text/csv",
            use_container_width=True,
        )

    if successful:
        st.subheader("Pinterest-ready Media URLs")

        st.code(
            "\n".join(r["ImageKit URL"] for r in successful),
            language=None,
        )

    if failed:
        st.error(
            "Some files failed. Download failed_uploads.csv to see the error for each image."
        )


# ----------------------------
# Run instructions
# ----------------------------

with st.expander("How to run this app"):
    st.markdown(
        """
Open PowerShell in the folder containing this file and run:

```powershell
py -m pip install streamlit requests
py -m streamlit run imagekit.py
```

Then Streamlit opens the uploader in your browser.

### Direct image mode
Choose **Direct images** → select all images → Start Upload.

### ZIP mode
Choose **ZIP file** → upload your ZIP → optionally preserve folders → Start Upload.

### Pinterest use
Use:

```text
Media URL = generated ImageKit URL
Link      = your normal website article URL
```

Example:

```text
Media URL:
https://ik.imagekit.io/your_id/pinterest/image.jpg

Link:
https://geminiaipromptss.com/article/


```
"""
    )
