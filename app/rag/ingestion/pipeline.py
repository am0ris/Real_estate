"""
RAG Ingestion Pipeline.

Responsibilities:

    file
        -> file type detection
        -> loader
        -> extraction
        -> normalization
        -> metadata
        -> chunking
        -> chunk records

Architecture notes:

    - Text-based files are loaded and normalized before chunking.
    - Structured spreadsheets are processed row-by-row.
    - For property datasets, JSON stored in the `metadata` column is
      parsed before field mapping.
    - Each property keeps its own canonical metadata and identity.
    - Images are processed by OCR and visual CV before normalization.
    - Embeddings and vector storage are intentionally NOT handled here.
    - The output of this pipeline is ready for the indexing layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from app.rag.ingestion import (
    load_image,
    parse_office,
    parse_pdf,
    parse_spreadsheet,
    parse_text,
)
from app.rag.ingestion.chunker import chunk_text
from app.rag.ingestion.metadata import (
    add_chunk_metadata,
    build_metadata,
)
from app.helpers.locations import canonical_district
from app.rag.normalization.field_mapper import FieldMapper
from app.rag.normalization.property_normalizer import PropertyNormalizer


logger = logging.getLogger(__name__)


# ============================================================
# Types
# ============================================================

Loader = Callable[[str], Any]


# ============================================================
# Supported file types
# ============================================================

LOADER_REGISTRY: dict[str, Loader] = {
    ".pdf": parse_pdf,
    ".docx": parse_office,
    ".pptx": parse_office,
    ".txt": parse_text,
    ".md": parse_text,
    ".markdown": parse_text,
    ".csv": parse_spreadsheet,
    ".xlsx": parse_spreadsheet,
    ".xls": parse_spreadsheet,
    ".jpg": load_image,
    ".jpeg": load_image,
    ".png": load_image,
    ".webp": load_image,
    ".bmp": load_image,
    ".tiff": load_image,
    ".tif": load_image,
}


FILE_TYPE_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "office",
    ".pptx": "office",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
    ".csv": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".bmp": "image",
    ".tiff": "image",
    ".tif": "image",
}


# ============================================================
# Normalization components
# ============================================================

_property_normalizer: PropertyNormalizer | None = None
_field_mapper: FieldMapper | None = None


def _get_property_normalizer() -> PropertyNormalizer:
    """Lazily initialize the property normalizer."""
    global _property_normalizer

    if _property_normalizer is None:
        _property_normalizer = PropertyNormalizer()

    return _property_normalizer


def _get_field_mapper() -> FieldMapper:
    """Lazily initialize the structured field mapper."""
    global _field_mapper

    if _field_mapper is None:
        _field_mapper = FieldMapper(
            preserve_unknown=True
        )

    return _field_mapper


# ============================================================
# File helpers
# ============================================================

def detect_file_type(file_path: str) -> str:
    """
    Detect the logical file type from its extension.

    Returns:
        pdf, office, text, spreadsheet, image, or unknown.
    """
    extension = Path(file_path).suffix.lower()
    return FILE_TYPE_MAP.get(extension, "unknown")


def is_supported(file_path: str) -> bool:
    """Check whether the file extension is supported."""
    extension = Path(file_path).suffix.lower()
    return extension in LOADER_REGISTRY


# ============================================================
# ID helpers
# ============================================================

def _build_chunk_id(
    file_path: Path,
    chunk_index: int,
    property_id: str | None = None,
    record_index: int | None = None,
) -> str:
    """
    Build a deterministic identifier for a chunk.

    Identity includes:

        property_id
        file path
        record index
        chunk index
    """
    identity = (
        f"{property_id or 'no-property'}"
        f"::{file_path.resolve()}"
        f"::record-{record_index if record_index is not None else 'document'}"
        f"::chunk-{chunk_index}"
    )

    return hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()


def _extract_property_id(
    mapped_fields: dict[str, Any],
    row: dict[str, Any],
    *,
    fallback_index: int,
) -> str:
    """
    Extract a property identifier from a structured row.

    The canonical `property_id` is preferred. If unavailable,
    common source column names are checked. Finally, a deterministic
    row-based identifier is generated.
    """
    candidates = [
        mapped_fields.get("property_id"),
        row.get("property_id"),
        row.get("Property ID"),
        row.get("propertyId"),
        row.get("id"),
        row.get("ID"),
    ]

    for value in candidates:
        if value is None:
            continue

        value_text = str(value).strip()

        if value_text:
            return value_text

    return f"row-{fallback_index}"


# ============================================================
# Metadata / normalization helpers
# ============================================================

def _merge_metadata(
    base_metadata: dict[str, Any],
    normalized_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Merge canonical normalized property fields into document metadata.
    """
    metadata = dict(base_metadata)

    if normalized_fields:
        for key, value in normalized_fields.items():
            if value is not None:
                metadata[key] = value

    return metadata


def _normalize_text(text: str) -> dict[str, Any]:
    """
    Extract canonical property fields from unstructured text.
    """
    normalizer = _get_property_normalizer()
    normalized = normalizer.normalize_to_dict(text)

    if not isinstance(normalized, dict):
        logger.warning(
            "PropertyNormalizer returned unexpected type | type=%s",
            type(normalized).__name__,
        )
        return {}

    return normalized


def _is_floorplan_image(path: Path, metadata: dict[str, Any]) -> bool:
    """Recognize floor plans explicitly, with a filename fallback."""
    kind = str(metadata.get("image_kind", "")).strip().lower()
    if kind in {"floorplan", "floor_plan", "floor-plan", "layout", "sketch"}:
        return True
    if kind in {"photo", "property_photo", "listing_photo"}:
        return False

    source_name = str(metadata.get("original_file_name") or path.name).lower()
    return bool(re.search(r"(?:floor[ _-]?plan|layout|unit[ _-]?sketch)", source_name))


def _visual_features_text(features: Any) -> str:
    """Format CV attributes as searchable text for chunking and embedding."""
    details: list[str] = []
    if features.caption:
        details.append(f"Visual description: {features.caption}")
    if features.room_type:
        details.append(f"Visually identified room type: {features.room_type}")
    if features.condition:
        details.append(f"Visually estimated condition: {features.condition}")
    if features.style_tags:
        details.append(f"Visual style: {', '.join(features.style_tags)}")
    if features.detected_amenities:
        details.append(f"Visually detected amenities: {', '.join(features.detected_amenities)}")
    return "\n".join(details)


# ============================================================
# Structured metadata parsing
# ============================================================

def _parse_structured_metadata(
    value: Any,
) -> dict[str, Any]:
    """
    Parse a structured `metadata` value.

    Property CSV files in this project store their real property
    fields as a JSON object inside the `metadata` column.

    Examples:

        '{"property_id": 123, "type": "Villa", "price_egp": 7800000}'

    or:

        {"property_id": 123, "type": "Villa"}

    Invalid or non-object values return an empty dictionary.
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return dict(value)

    if not isinstance(value, str):
        return {}

    raw = value.strip()

    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Structured metadata JSON parsing failed"
        )
        return {}

    if not isinstance(parsed, dict):
        logger.warning(
            "Structured metadata JSON is not an object | type=%s",
            type(parsed).__name__,
        )
        return {}

    return parsed


def _prepare_structured_row(
    row: dict[str, Any],
) -> dict[str, Any]:
    """
    Flatten a property row before field mapping.

    Example input:

        {
            "id": 501189976,
            "text": "Villa for sale ...",
            "metadata": "{\"property_id\": 501189976, ...}"
        }

    Result:

        {
            "id": 501189976,
            "text": "Villa for sale ...",
            "property_id": 501189976,
            "type": "Villa",
            "price_egp": 7800000,
            ...
        }

    The original `text` and `metadata` fields are preserved.
    """
    prepared = dict(row)

    raw_metadata = row.get("metadata")
    parsed_metadata = _parse_structured_metadata(raw_metadata)

    if parsed_metadata:
        prepared.update(parsed_metadata)

    return prepared


def _normalize_structured_row(
    row: dict[str, Any],
) -> dict[str, Any]:
    """
    Map one structured spreadsheet row to canonical property fields.

    The row is first flattened so JSON metadata fields become visible
    to the FieldMapper.
    """
    prepared_row = _prepare_structured_row(row)

    mapper = _get_field_mapper()
    result = mapper.map_fields(prepared_row)

    if not isinstance(result.mapped, dict):
        logger.warning(
            "FieldMapper returned unexpected mapped type | type=%s",
            type(result.mapped).__name__,
        )
        return {}

    normalized = {
        key: value
        for key, value in result.mapped.items()
        if value is not None and value != ""
    }
    # One spelling per place ("AinSukhna" / "Ain Sokhna" -> "Ain Sukhna"): the assistant filters
    # on this field with an exact match (see app/helpers/locations.py).
    if normalized.get("district"):
        normalized["district"] = canonical_district(normalized["district"])

    logger.info(
        "Structured row normalized | mapped=%d | unmapped=%d",
        len(normalized),
        len(result.unmapped),
    )

    return normalized


def _get_structured_row_text(
    row: dict[str, Any],
) -> str:
    """
    Extract the original searchable text from a structured row.

    If a `text` column exists, it is preferred because it is already
    written as a natural-language property description.

    Otherwise, a textual representation of the row is generated.
    """
    text = row.get("text")

    if isinstance(text, str) and text.strip():
        return text.strip()

    prepared_row = _prepare_structured_row(row)

    lines: list[str] = []

    for key, value in prepared_row.items():
        if key == "metadata":
            continue

        if value is None:
            continue

        value_text = str(value).strip()

        if not value_text:
            continue

        lines.append(f"{key}: {value_text}")

    return "\n".join(lines).strip()


# ============================================================
# Structured row ingestion
# ============================================================

def _build_structured_row_chunk(
    *,
    path: Path,
    row: dict[str, Any],
    row_index: int,
    document_metadata: dict[str, Any],
    property_id: str | None,
    chunk_size: int,
    chunk_overlap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Convert one spreadsheet row into independent chunk records.

    Returns:
        (chunk_records, normalized_fields)
    """
    normalized_fields = _normalize_structured_row(row)

    prepared_row = _prepare_structured_row(row)

    row_property_id = _extract_property_id(
        normalized_fields,
        prepared_row,
        fallback_index=row_index,
    )

    # Explicit function argument takes precedence.
    if property_id:
        row_property_id = property_id

    row_metadata = dict(document_metadata)

    row_metadata["property_id"] = row_property_id
    row_metadata["record_index"] = row_index

    row_metadata = _merge_metadata(
        row_metadata,
        normalized_fields,
    )

    text = _get_structured_row_text(row)

    if not text:
        return [], normalized_fields

    chunks = chunk_text(
        text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    if not chunks:
        return [], normalized_fields

    total_chunks = len(chunks)

    chunk_records: list[dict[str, Any]] = []

    for chunk_index, chunk in enumerate(chunks):
        chunk_metadata = add_chunk_metadata(
            row_metadata,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )

        # Keep property identity explicit in every chunk.
        chunk_metadata["property_id"] = row_property_id
        chunk_metadata["record_index"] = row_index

        chunk_id = _build_chunk_id(
            path,
            chunk_index,
            row_property_id,
            record_index=row_index,
        )

        chunk_records.append(
            {
                "id": chunk_id,
                "text": chunk,
                "metadata": chunk_metadata,
            }
        )

    return chunk_records, normalized_fields


# ============================================================
# Single file ingestion
# ============================================================

def ingest_file(
    file_path: str,
    *,
    property_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    chunk_size: int = 1500,
    chunk_overlap: int = 100,
) -> dict[str, Any]:
    """
    Ingest a single file.

    Embeddings and vector storage are intentionally NOT handled here.
    """
    path = Path(file_path)

    logger.info(
        "Starting file ingestion | file=%s",
        file_path,
    )

    # --------------------------------------------------------
    # Validate file
    # --------------------------------------------------------

    if not path.exists():
        logger.error(
            "File does not exist | file=%s",
            file_path,
        )
        raise FileNotFoundError(
            f"File not found: {file_path}"
        )

    if not path.is_file():
        logger.error(
            "Path is not a file | path=%s",
            file_path,
        )
        raise ValueError(
            f"Path is not a file: {file_path}"
        )

    # --------------------------------------------------------
    # Detect file type
    # --------------------------------------------------------

    extension = path.suffix.lower()

    if extension not in LOADER_REGISTRY:
        logger.warning(
            "Unsupported file type | file=%s | extension=%s",
            file_path,
            extension,
        )
        return {
            "status": "unsupported",
            "file": str(path),
            "file_type": "unknown",
            "reason": f"Unsupported extension: {extension}",
            "chunks": [],
            "chunks_count": 0,
        }

    source_type = detect_file_type(str(path))

    logger.info(
        "Detected file type | file=%s | source_type=%s",
        file_path,
        source_type,
    )

    # --------------------------------------------------------
    # Load file
    # --------------------------------------------------------

    loader = LOADER_REGISTRY[extension]

    try:
        raw_content = loader(str(path))
    except Exception:
        logger.exception(
            "File loading failed | file=%s",
            file_path,
        )
        raise

    # --------------------------------------------------------
    # Build technical metadata
    # --------------------------------------------------------

    document_metadata = build_metadata(
        str(path),
        source_type=source_type,
        property_id=property_id,
        extra_metadata=extra_metadata,
    )

    # ========================================================
    # IMAGE FLOW
    # ========================================================

    if source_type == "image":
        from app.rag.ocr.extractor import OCRExtractor
        from app.rag.ocr.post_processor import OCRPostProcessor
        from app.rag.cv.vision_extractor import extract_visual_features

        extracted_text = OCRExtractor().extract_from_image(path)
        ocr_text = OCRPostProcessor().process(extracted_text).text
        source_name = str(document_metadata.get("original_file_name") or path.name)
        content_parts = [ocr_text] if ocr_text else []
        try:
            visual_features = extract_visual_features(path, source_file=source_name)
            content_parts.append(_visual_features_text(visual_features))
            document_metadata.update({
                "cv_applied": True,
                "visual_room_type": visual_features.room_type,
                "visual_condition": visual_features.condition,
                "visual_style_tags": visual_features.style_tags,
                "visual_amenities": visual_features.detected_amenities,
                "visual_caption": visual_features.caption,
                "visual_confidence": visual_features.confidence,
            })
        except Exception as error:
            logger.exception("Image CV extraction failed | file=%s", path)
            document_metadata["cv_applied"] = False
            document_metadata["cv_error"] = str(error)

        if _is_floorplan_image(path, document_metadata):
            from app.rag.cv.floorplan_parser import parse_floorplan

            try:
                floorplan = parse_floorplan(path, source_file=source_name)
                document_metadata["floorplan_analysis"] = {
                    "room_count_estimate": floorplan.room_count_estimate,
                    "image_size": list(floorplan.image_size),
                    "confidence": floorplan.confidence,
                    "notes": floorplan.notes,
                    "regions": [
                        {
                            "label": region.label,
                            "bounding_box": list(region.bounding_box),
                            "area_px": region.area_px,
                            "approx_shape": region.approx_shape,
                        }
                        for region in floorplan.regions
                    ],
                }
                content_parts.append(
                    f"Floor plan analysis: estimated {floorplan.room_count_estimate} room-like regions."
                )
            except Exception as error:
                logger.exception("Floor plan analysis failed | file=%s", path)
                document_metadata["floorplan_analysis_error"] = str(error)

        raw_content = "\n\n".join(content_parts)
        document_metadata["ocr_applied"] = True

    # ========================================================
    # SPREADSHEET FLOW
    # ========================================================

    if source_type == "spreadsheet":
        if not isinstance(raw_content, list):
            logger.error(
                "Spreadsheet loader returned unexpected content type | "
                "file=%s | type=%s",
                file_path,
                type(raw_content).__name__,
            )
            raise TypeError(
                "Expected spreadsheet loader to return list[dict], "
                f"got {type(raw_content).__name__}"
            )

        rows = [
            row
            for row in raw_content
            if isinstance(row, dict)
        ]

        if not rows:
            logger.warning(
                "No spreadsheet records extracted | file=%s",
                file_path,
            )

            return {
                "status": "empty",
                "file": str(path),
                "file_type": source_type,
                "property_id": property_id,
                "chunks": [],
                "chunks_count": 0,
                "metadata": document_metadata,
                "records": [],
            }

        all_chunk_records: list[dict[str, Any]] = []
        normalized_records: list[dict[str, Any]] = []

        for row_index, row in enumerate(rows, start=1):
            row_chunks, normalized_fields = (
                _build_structured_row_chunk(
                    path=path,
                    row=row,
                    row_index=row_index,
                    document_metadata=document_metadata,
                    property_id=property_id,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
            )

            if not row_chunks:
                logger.warning(
                    "Spreadsheet row produced no chunks | "
                    "file=%s | row=%d",
                    file_path,
                    row_index,
                )
                continue

            row_property_id = row_chunks[0]["metadata"].get(
                "property_id"
            )

            normalized_records.append(
                {
                    "record_index": row_index,
                    "property_id": row_property_id,
                    "fields": normalized_fields,
                }
            )

            all_chunk_records.extend(row_chunks)

        if not all_chunk_records:
            logger.warning(
                "Spreadsheet produced no searchable records | file=%s",
                file_path,
            )

            return {
                "status": "empty",
                "file": str(path),
                "file_type": source_type,
                "property_id": property_id,
                "chunks": [],
                "chunks_count": 0,
                "metadata": document_metadata,
                "records": rows,
                "normalized_records": normalized_records,
            }

        logger.info(
            "Spreadsheet processed row-by-row | "
            "file=%s | records=%d | chunks=%d",
            file_path,
            len(normalized_records),
            len(all_chunk_records),
        )

        return {
            "status": "success",
            "file": str(path),
            "file_type": source_type,
            "property_id": property_id,
            "chunks": all_chunk_records,
            "chunks_count": len(all_chunk_records),
            "metadata": document_metadata,
            "records": rows,
            "normalized_records": normalized_records,
        }

    # ========================================================
    # TEXT FLOW
    # ========================================================

    if not isinstance(raw_content, str):
        logger.error(
            "Loader returned unsupported content type | "
            "file=%s | type=%s",
            file_path,
            type(raw_content).__name__,
        )

        raise TypeError(
            "Expected loader to return str, "
            f"got {type(raw_content).__name__}"
        )

    text = raw_content.strip()

    # --------------------------------------------------------
    # Empty content
    # --------------------------------------------------------

    if not text:
        logger.warning(
            "No text extracted from file | file=%s",
            file_path,
        )

        return {
            "status": "empty",
            "file": str(path),
            "file_type": source_type,
            "property_id": property_id,
            "chunks": [],
            "chunks_count": 0,
            "metadata": document_metadata,
        }

    logger.info(
        "Text extracted | file=%s | characters=%d",
        file_path,
        len(text),
    )

    # --------------------------------------------------------
    # Property normalization
    # --------------------------------------------------------

    normalized_fields = _normalize_text(text)

    document_metadata = _merge_metadata(
        document_metadata,
        normalized_fields,
    )

    logger.info(
        "Text normalization completed | "
        "file=%s | fields=%d",
        file_path,
        len(normalized_fields),
    )

    # ========================================================
    # CHUNKING
    # ========================================================

    chunks = chunk_text(
        text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    if not chunks:
        logger.warning(
            "No chunks generated | file=%s",
            file_path,
        )

        return {
            "status": "empty",
            "file": str(path),
            "file_type": source_type,
            "property_id": property_id,
            "chunks": [],
            "chunks_count": 0,
            "metadata": document_metadata,
            "normalized_fields": normalized_fields,
        }

    logger.info(
        "Chunks generated | file=%s | chunks=%d",
        file_path,
        len(chunks),
    )

    # ========================================================
    # BUILD CHUNK RECORDS
    # ========================================================

    total_chunks = len(chunks)
    chunk_records: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks):
        chunk_metadata = add_chunk_metadata(
            document_metadata,
            chunk_index=index,
            total_chunks=total_chunks,
        )

        chunk_id = _build_chunk_id(
            path,
            index,
            property_id,
            record_index=None,
        )

        chunk_records.append(
            {
                "id": chunk_id,
                "text": chunk,
                "metadata": chunk_metadata,
            }
        )

    # ========================================================
    # RETURN INGESTION RESULT
    # ========================================================

    logger.info(
        "File ingestion completed | "
        "file=%s | chunks=%d",
        file_path,
        total_chunks,
    )

    return {
        "status": "success",
        "file": str(path),
        "file_type": source_type,
        "property_id": property_id,
        "chunks": chunk_records,
        "chunks_count": total_chunks,
        "metadata": document_metadata,
        "normalized_fields": normalized_fields,
    }


# ============================================================
# Directory ingestion
# ============================================================

def ingest_directory(
    directory_path: str,
    *,
    property_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    chunk_size: int = 1500,
    chunk_overlap: int = 100,
    recursive: bool = False,
) -> dict[str, Any]:
    """
    Ingest all supported files inside a directory.

    Embeddings and vector storage are not performed here.
    """
    directory = Path(directory_path)

    logger.info(
        "Starting directory ingestion | directory=%s",
        directory_path,
    )

    # --------------------------------------------------------
    # Validate directory
    # --------------------------------------------------------

    if not directory.exists():
        raise FileNotFoundError(
            f"Directory not found: {directory_path}"
        )

    if not directory.is_dir():
        raise ValueError(
            f"Path is not a directory: {directory_path}"
        )

    # --------------------------------------------------------
    # Collect supported files
    # --------------------------------------------------------

    if recursive:
        files = [
            path
            for path in directory.rglob("*")
            if path.is_file()
            and is_supported(str(path))
        ]
    else:
        files = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and is_supported(str(path))
        ]

    files.sort(
        key=lambda path: str(path).lower()
    )

    logger.info(
        "Supported files discovered | "
        "directory=%s | files=%d",
        directory_path,
        len(files),
    )

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    total = len(files)
    successful = 0
    skipped = 0
    failed = 0
    total_chunks = 0

    results: list[dict[str, Any]] = []

    # --------------------------------------------------------
    # Process files
    # --------------------------------------------------------

    for path in files:
        logger.info(
            "Processing file | file=%s",
            path,
        )

        try:
            result = ingest_file(
                str(path),
                property_id=property_id,
                extra_metadata=extra_metadata,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )

            results.append(result)

            status = result.get("status")

            if status == "success":
                successful += 1

                total_chunks += int(
                    result.get(
                        "chunks_count",
                        0,
                    )
                )

            elif status in {
                "unsupported",
                "empty",
                "image_loaded",
            }:
                skipped += 1

        except Exception as exc:
            failed += 1

            logger.exception(
                "File ingestion failed | file=%s",
                path,
            )

            results.append(
                {
                    "status": "failed",
                    "file": str(path),
                    "error": str(exc),
                }
            )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = {
        "status": "completed",
        "directory": str(directory),
        "total_files": total,
        "successful": successful,
        "skipped": skipped,
        "failed": failed,
        "total_chunks": total_chunks,
        "results": results,
    }

    logger.info(
        "Directory ingestion completed | "
        "directory=%s | total=%d | "
        "successful=%d | skipped=%d | "
        "failed=%d | chunks=%d",
        directory_path,
        total,
        successful,
        skipped,
        failed,
        total_chunks,
    )

    return summary
