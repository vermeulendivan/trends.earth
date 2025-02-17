"""
Code for calculating all three SDG 15.3.1 sub-indicators.
"""
# Copyright 2017 Conservation International
import random
import tempfile
from pathlib import Path

import te_algorithms.gdal.land_deg.config as ld_config
from te_algorithms.api import util
from te_algorithms.gdal.land_deg.land_deg_recode import rasterize_error_recode
from te_algorithms.gdal.land_deg.land_deg_recode import recode_errors
from te_schemas import algorithms
from te_schemas import jobs
from te_schemas.aoi import AOI
from te_schemas.error_recode import ErrorRecodePolygons
from te_schemas.productivity import ProductivityMode
from te_schemas.results import Band
from te_schemas.results import JsonResults
from te_schemas.results import RasterResults

S3_PREFIX_RAW_DATA = "prais4-raw"
S3_BUCKET_INPUT = "trends.earth-private"
S3_REGION = "us-east-1"
S3_BUCKET_USER_DATA = "trends.earth-users"
ERROR_RECODE_BAND_NAME = "Error recode"
RECODE_SCRIPT = algorithms.ExecutionScript.Schema().load(
    {
        "name": "sdg-15-3-1-error-recode",
        "version": "1.13",
        "run_mode": algorithms.AlgorithmRunMode.LOCAL,
    }
)


def calculate_error_recode(
    aoi,
    error_polygons,
    script_name,
    iso,
    band_name,
    filters,
    boundary_dataset,
    substr_regexs,
    write_tifs,
    EXECUTION_ID,
    logger,
):

    filename_base = iso
    filename_base += "_" + boundary_dataset
    filename_base += "_" + script_name
    s3_prefix = f"{S3_PREFIX_RAW_DATA}/" + filename_base
    logger.info(f"Looking for prefix {s3_prefix}")
    try:
        input_job = util.get_job_json_from_s3(
            s3_prefix=s3_prefix, s3_bucket=S3_BUCKET_INPUT, substr_regexs=substr_regexs
        )
    except IndexError as exc:
        logger.error(f"Failed to load input job from prefix {s3_prefix}: {exc}")
        raise exc

    error_recode_tif = tempfile.NamedTemporaryFile(
        suffix="_error_recode.tif", delete=False
    ).name
    rasterize_error_recode(error_recode_tif, input_job.results.uri.uri, error_polygons)
    error_recode_band = Band(
        name=ERROR_RECODE_BAND_NAME,
        metadata={},
        no_data_value=int(ld_config.NODATA_VALUE),
        activated=True,
    )

    try:
        input_band = util.get_band_by_name(
            input_job,
            band_name,
            filters,
        )
    except IndexError:
        logger.exception("Failed to load band name %s".format(band_name))

    recode_params = {
        "write_tifs": write_tifs,
        "local_context": jobs.JobLocalContext.Schema().dump(input_job.local_context),
        "task_name": input_job.task_name,
        "metadata": input_band.band.metadata,
        "task_notes": input_job.task_notes,
        "layer_input_band_path": str(input_job.results.uri.uri),
        "layer_input_band": Band.Schema().dump(input_band.band),
        "layer_input_band_index": input_band.band_number,
        "layer_error_recode_path": str(error_recode_tif),
        "layer_error_recode_band": Band.Schema().dump(error_recode_band),
        "layer_error_recode_band_index": 1,
        "error_polygons": ErrorRecodePolygons.Schema().dump(error_polygons),
        "input_job": jobs.Job.Schema().dump(input_job),
        "aoi": AOI.Schema().dump(aoi),
    }

    logger.info("Starting error recoding calculation")
    with tempfile.TemporaryDirectory() as temp_dir:
        recode_params["output_path"] = Path(temp_dir) / input_job.results.uri.uri.stem
        results = recode_errors(recode_params)

        # Save the input job to results as well
        results.data = {
            "report": results.data,
            "input_job": jobs.Job.Schema().dump(input_job),
            "input_band_index": input_band.band_number,
        }

        if write_tifs:
            logger.info("Writing tifs")
            results = util.write_results_to_s3_cog(
                results,
                aoi,
                filename_base=EXECUTION_ID,
                s3_prefix="prais-4",
                s3_bucket=S3_BUCKET_USER_DATA,
            )

    if isinstance(results, RasterResults):
        results = RasterResults.Schema().dump(results)

    elif isinstance(results, JsonResults):
        results = JsonResults.Schema().dump(results)
    else:
        raise Exception

    return results


def run(params, logger):
    """."""
    logger.debug("Loading parameters.")

    # Check the ENV. Are we running this locally or in prod?

    if params.get("ENV") == "dev":
        EXECUTION_ID = str(random.randint(1000000, 99999999))
    else:
        EXECUTION_ID = params.get("EXECUTION_ID", None)
    logger.debug(f"Execution ID is {EXECUTION_ID}")

    aoi = AOI(params["aoi"])
    error_polygons = ErrorRecodePolygons.Schema().load(params["error_polygons"])
    script_name = params["script_name"]
    iso = params["iso"]
    band_name = params["band_name"]
    filters = params["filters"]
    boundary_dataset = params.get("boundary_dataset", "UN")
    productivity_dataset = params.get(
        "productivity_dataset", ProductivityMode.JRC_5_CLASS_LPD.value
    )
    write_tifs = params.get("write_tifs", False)

    substr_regexs = params.get("substr_regexs", [])
    substr_regexs.append(productivity_dataset)

    return calculate_error_recode(
        aoi,
        error_polygons,
        script_name,
        iso,
        band_name,
        filters,
        boundary_dataset,
        substr_regexs,
        write_tifs,
        EXECUTION_ID,
        logger,
    )
