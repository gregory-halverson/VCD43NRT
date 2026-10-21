using Dates
using Statistics
using ArchGDAL
using Glob
using LinearAlgebra

# This script is the Julia entrypoint expected by the Python VNP43NRT wrapper.
# It loads the actual BRDF implementation from src/VNP43NRT.jl and runs the
# staged GeoTIFF stack through the real NRT_BRDF solver instead of writing
# placeholder values.
#
# Expected CLI:
#   julia --project=.../VNP43NRT process_VNP43NRT.jl \
#       band h v tile_width_cells start_date end_date \
#       reflectance_directory solar_zenith_directory sensor_zenith_directory \
#       relative_azimuth_directory SZA_filename output_directory

include(joinpath(@__DIR__, "src", "VNP43NRT.jl"))
using .VNP43NRT

function read_raster_matrix(path::AbstractString)
    dataset = ArchGDAL.read(path)
    band = ArchGDAL.getband(dataset, 1)
    data = ArchGDAL.read(band)

    if isa(data, AbstractArray) && ndims(data) == 3
        data = data[:, :, 1]
    end

    return Matrix{Float64}(data)
end

function stack_rasters(directory::AbstractString)
    files = sort(Glob.glob(joinpath(directory, "*.tif")))
    if isempty(files)
        error("no GeoTIFF files were found in $(directory)")
    end

    matrices = [read_raster_matrix(path) for path in files]
    shape = size(matrices[1])
    flattened = [vec(Matrix{Float64}(m)) for m in matrices]
    stacked = hcat(flattened...)

    return stacked, shape
end

function write_raster_matrix(path::AbstractString, data::AbstractMatrix)
    m, n = size(data)
    driver = ArchGDAL.getdriver("GTiff")
    dataset = ArchGDAL.create(path, width=n, height=m; driver=driver, nbands=1, dtype=Float32)
    ArchGDAL.write!(dataset, Matrix{Float32}(data), 1)
    ArchGDAL.destroy(dataset)
    return path
end

function write_outputs(output_directory::AbstractString, date_label::AbstractString, shape::Tuple{Int,Int}, result::Matrix{Float64})
    mkpath(output_directory)
    output_names = [
        "WSA" => 1,
        "BSA" => 2,
        "NBAR" => 3,
        "WSA_SE" => 4,
        "BSA_SE" => 5,
        "NBAR_SE" => 6,
        "BRDF_SE" => 7,
        "BRDF_R2" => 8,
        "count" => 9,
    ]

    for (name, idx) in output_names
        filename = joinpath(output_directory, "$(date_label)_$(name).tif")
        arr = reshape(result[:, idx], shape)
        write_raster_matrix(filename, arr)
    end

    return output_directory
end

function main()
    if length(ARGS) < 12
        error("process_VNP43NRT.jl requires 12 arguments: band h v tile_width_cells start_date end_date reflectance_directory solar_zenith_directory sensor_zenith_directory relative_azimuth_directory SZA_filename output_directory")
    end

    band = ARGS[1]
    h = parse(Int, ARGS[2])
    v = parse(Int, ARGS[3])
    tile_width_cells = parse(Int, ARGS[4])
    start_date = Date(ARGS[5])
    end_date = Date(ARGS[6])
    reflectance_directory = ARGS[7]
    solar_zenith_directory = ARGS[8]
    sensor_zenith_directory = ARGS[9]
    relative_azimuth_directory = ARGS[10]
    SZA_filename = ARGS[11]
    output_directory = ARGS[12]

    reflectance_stack, shape = stack_rasters(reflectance_directory)
    solar_zenith_stack, _ = stack_rasters(solar_zenith_directory)
    sensor_zenith_stack, _ = stack_rasters(sensor_zenith_directory)
    relative_azimuth_stack, _ = stack_rasters(relative_azimuth_directory)
    solar_noon = vec(read_raster_matrix(SZA_filename))

    if size(reflectance_stack, 1) != size(solar_zenith_stack, 1) ||
       size(reflectance_stack, 1) != size(sensor_zenith_stack, 1) ||
       size(reflectance_stack, 1) != size(relative_azimuth_stack, 1)
        error("staged GeoTIFF stacks do not share the same pixel count")
    end

    if length(solar_noon) != size(reflectance_stack, 1)
        error("SZA noon raster length $(length(solar_noon)) does not match BRDF pixel count $(size(reflectance_stack, 1))")
    end

    results = NRT_BRDF_all(reflectance_stack, solar_zenith_stack, sensor_zenith_stack, relative_azimuth_stack, solar_noon, true, 1.87)
    target_date = end_date
    write_outputs(output_directory, Dates.format(target_date, "yyyy-mm-dd"), shape, results)

    println("VNP43NRT BRDF completed for band $(band) at tile h=$(h), v=$(v), tile_width_cells=$(tile_width_cells)")
    println("Inputs: $(reflectance_directory), $(solar_zenith_directory), $(sensor_zenith_directory), $(relative_azimuth_directory)")
    println("SZA file: $(SZA_filename)")
    println("Outputs: $(output_directory)")
end

main()
