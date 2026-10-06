"""Old import path. Use tools.buildings."""
try:
    from .buildings import (
        BuildingMask,
        adjust_stem,
        arguments,
        automatic_buildings,
        from_args,
        number,
        optional_year,
        options,
        overture_features,
        overture_files,
        prepare_canopy,
        public_json,
        raster_years,
    )
except ImportError:
    from buildings import (
        BuildingMask,
        adjust_stem,
        arguments,
        automatic_buildings,
        from_args,
        number,
        optional_year,
        options,
        overture_features,
        overture_files,
        prepare_canopy,
        public_json,
        raster_years,
    )
