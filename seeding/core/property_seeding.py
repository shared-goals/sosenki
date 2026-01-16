"""Property parsing and creation utilities for database seeding.

Handles property parsing, "Доп" column splitting, selective attribute inheritance,
and type mapping for auxiliary properties. All configuration loaded from seeding.json.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from seeding.config.seeding_config import SeedingConfig
from seeding.core.errors import DataValidationError
from src.models.property import Property
from src.models.user import User
from src.utils.parsers import (
    parse_boolean,
    parse_russian_currency,
    parse_russian_percentage,
)


def parse_property_row(row_dict: dict[str, str], owner: User) -> list[dict[str, Any]]:
    """
    Parse a row from named range into one or more Property records.

    Uses three-phase approach:
    Phase 1: Extract fields from row using column mappings from config
    Phase 2: Apply default attributes
    Phase 3: Apply transformations (additional properties from source column)

    Creates main property from row data, plus additional properties from
    source column if present.

    Args:
        row_dict: Dictionary mapping column names to cell values
        owner: User instance (property owner)

    Returns:
        List of dicts with Property attributes (main + additional)
        Empty list if row should be skipped

    Raises:
        DataValidationError: On validation errors
    """
    logger = logging.getLogger("sosenki.seeding.properties")
    properties: list[dict[str, Any]] = []

    # Load configuration
    config = SeedingConfig.load()

    try:
        # PHASE 1: Extract fields from main property row
        main_field_mappings = config.get_property_field_mappings("main")
        property_name_column = main_field_mappings.get("property_name_column") or ""
        property_name = row_dict.get(property_name_column, "").strip()
        if not property_name:
            logger.warning("Skipping row: empty property_name (%s column)", property_name_column)
            return []

        # Parse numeric fields
        try:
            share_weight_column = main_field_mappings.get("share_weight_column") or ""
            share_weight = parse_russian_percentage(row_dict.get(share_weight_column, ""))
        except ValueError as e:
            logger.warning(f"Invalid share_weight format: {e}, skipping row")
            return []

        try:
            sale_price_column = main_field_mappings.get("sale_price_column") or ""
            sale_price_decimal = parse_russian_currency(row_dict.get(sale_price_column, ""))
            # Convert Decimal to int for integer price storage
            sale_price = int(sale_price_decimal) if sale_price_decimal is not None else None
        except ValueError as e:
            logger.warning(f"Invalid sale_price format: {e}, skipping row")
            return []

        # Extract other field values
        type_column = main_field_mappings.get("type_column") or ""
        ready_column = main_field_mappings.get("is_ready_column") or ""
        tenant_column = main_field_mappings.get("is_for_tenant_column") or ""
        photo_column = main_field_mappings.get("photo_link_column") or ""

        # PHASE 2: Apply default attributes
        main_property: dict[str, Any] = {
            "owner_id": owner.id,
            "property_name": property_name,
            "type": row_dict.get(type_column, "").strip(),
            "share_weight": share_weight,
            "is_ready": parse_boolean(row_dict.get(ready_column, "")),
            "is_for_tenant": parse_boolean(row_dict.get(tenant_column, "")),
            "photo_link": row_dict.get(photo_column, "").strip() or None,
            "sale_price": sale_price,
            "is_active": True,
            "main_property_id": None,  # Main property has no parent
        }

        # Apply is_conservation transformation if code is in mapping, else use default
        main_is_conservation_mapping = config.get_main_property_is_conservation_mapping()
        is_conservation = main_is_conservation_mapping.get(property_name, True)
        main_property["is_conservation"] = is_conservation

        properties.append(main_property)
        logger.debug(f"Parsed main property: {property_name} (is_conservation={is_conservation})")

        # PHASE 3: Apply transformations (additional properties from "Доп" column)
        dop_source_column = config.get_dop_source_column()
        dop_value = row_dict.get(dop_source_column, "").strip()
        if dop_value:
            # Split by comma and create additional properties
            dop_codes = [code.strip() for code in dop_value.split(",")]

            # Get type mapping and default type for additional properties
            type_mapping = config.get_property_type_mapping()
            is_conservation_mapping = config.get_property_is_conservation_mapping()
            default_type = config.get_property_default_type()

            for code in dop_codes:
                if not code:  # Skip empty codes
                    continue

                # Map code to property type
                property_type = type_mapping.get(code, default_type)

                # Apply is_conservation transformation if code is in mapping, else use default
                is_conservation = is_conservation_mapping.get(code, True)

                # Additional property attributes (selective inheritance)
                additional_property: dict[str, Any] = {
                    "owner_id": owner.id,
                    "property_name": code,
                    "type": property_type,
                    "share_weight": None,
                    "is_ready": main_property["is_ready"],
                    "is_for_tenant": main_property["is_for_tenant"],
                    "photo_link": None,
                    "sale_price": None,
                    "is_active": True,
                    "is_conservation": is_conservation,
                    "main_property_id": None,  # Will be set in create_properties
                }
                properties.append(additional_property)
                logger.debug(
                    f"Parsed additional property from {dop_source_column}: {code} → {property_type} (is_conservation={is_conservation})"
                )

        return properties

    except Exception as e:
        raise DataValidationError(f"Failed to parse property row: {e}") from e


def create_properties(
    session: Session, property_dicts: list[dict[str, Any]], owner: User
) -> list[Property]:
    """
    Create Property records in database.

    The first property is treated as the main property. Additional properties
    (those created from "Доп" column) are linked to the main property via
    main_property_id foreign key.

    Args:
        session: SQLAlchemy session
        property_dicts: List of dicts with property attributes
        owner: Owner user for logging/reference

    Returns:
        List of created Property instances

    Raises:
        DataValidationError: On database errors
    """
    logger = logging.getLogger("sosenki.seeding.properties")
    created: list[Property] = []

    try:
        main_property: Property | None = None

        for idx, prop_dict in enumerate(property_dicts):
            # First property is the main property
            if idx == 0:
                prop = Property(**prop_dict)
                session.add(prop)
                session.flush()  # Get ID before full commit
                main_property = prop
                created.append(prop)
                logger.debug(f"Created main property: {prop.property_name} (id={prop.id})")
            else:
                # Additional properties reference the main property
                if main_property is not None:
                    prop_dict["main_property_id"] = main_property.id
                prop = Property(**prop_dict)
                session.add(prop)
                session.flush()
                created.append(prop)
                logger.debug(
                    f"Created additional property: {prop.property_name} "
                    f"(id={prop.id}, main_property_id={prop.main_property_id})"
                )

        return created

    except Exception as e:
        raise DataValidationError(f"Failed to create properties: {e}") from e
