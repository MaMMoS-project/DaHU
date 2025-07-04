"""
Functions for DEKTAK parsing
"""

from ..functions.functions_shared import *
from ..hdf5_compilers.hdf5compile_base import *

PROFIL_WRITER_VERSION = "0.2"


def read_header_from_dektak(file_path):
    header_dict = {}

    with open(file_path, "r") as file:
        lines = file.readlines()

    for line in lines[4:45]:
        split = line.strip().split(",")
        key, value = split[0], split[-1]
        if key == "TargetName":
            value = f"({split[-2].strip('(')},{split[-1].strip(')')})"
        header_dict[key] = value
    del header_dict["FullFilename"]
    return header_dict


def read_data_from_dektak(file_path, header_length=46):
    asc2d_dataframe = pd.read_csv(file_path, skiprows=header_length)
    asc2d_dataframe.rename(columns={" z(raw/unitless)": "profile"}, inplace=True)
    asc2d_dataframe.rename(columns={"y(um)": "distance"}, inplace=True)
    return asc2d_dataframe


def position_from_tuple(scan_number):
    pattern = r"\((\d+),(\d+)\)"
    match = re.search(pattern, scan_number)
    x = (int(match.group(2)) - 10) * 5  # Header tuple has the format (y,x)
    y = (10 - int(match.group(1))) * 5
    return x, y


def set_instrument_from_dict(header_dict, node):
    """
    Writes the contents of the moke_dict dictionary to the HDF5 node.

    Args:
        header_dict (dict): A dictionary containing the MOKE data and metadata, generated by the read_header_from_moke and read_data_from_moke functions.
        node (h5py.Group): The HDF5 group to write the data to.
    Returns:
        None
    """
    for key, value in header_dict.items():
        if isinstance(value, dict):
            set_instrument_from_dict(value, node.create_group(key))
        else:
            node[key] = value

    return None


def write_dektak_to_hdf5(hdf5_path, source_path, dataset_name=None, mode="a"):
    if isinstance(hdf5_path, str):
        hdf5_path = Path(hdf5_path)
    if isinstance(source_path, str):
        source_path = Path(source_path)

    if dataset_name is None:
        dataset_name = source_path.stem

    with h5py.File(hdf5_path, mode) as hdf5_file:
        # Create the root group for the measurement
        profil_group = hdf5_file.create_group(f"{dataset_name}")
        profil_group.attrs["HT_type"] = "profil"
        profil_group.attrs["instrument"] = "Bruker DektakXT"
        profil_group.attrs["profil_writer"] = PROFIL_WRITER_VERSION

        for file_name in safe_rglob(source_path, "*.asc2d"):
            file_path = source_path / file_name

            header_dict = read_header_from_dektak(file_path)
            asc2d_dataframe = read_data_from_dektak(file_path)
            scan_number = header_dict["TargetName"]

            x_pos, y_pos = position_from_tuple(scan_number)

            scan = profil_group.create_group(
                f"({round(float(x_pos), 1)},{round(float(y_pos),1)})"
            )
            scan.attrs["ignored"] = False

            # Instrument group for metadata
            instrument = scan.create_group("instrument")
            instrument.attrs["NX_class"] = "HTinstrument"
            instrument["x_pos"] = convertFloat(x_pos)
            instrument["y_pos"] = convertFloat(y_pos)
            instrument["x_pos"].attrs["units"] = "mm"
            instrument["y_pos"].attrs["units"] = "mm"

            set_instrument_from_dict(header_dict, instrument)

            # Measurement group for data
            data = scan.create_group("measurement")
            data.attrs["NX_class"] = "HTmeasurement"
            for col in asc2d_dataframe.columns:
                node = data.create_dataset(
                    col, data=np.array(asc2d_dataframe[col]), dtype="float"
                )
                if col == "profile":
                    node.attrs["unit"] = "nm"
                elif col == "distance":
                    node.attrs["unit"] = "μm"

    return None


def write_dektak_results_to_hdf5(position_group, results_dict, overwrite=True):
    if overwrite and "results" in position_group:
        del position_group["results"]

    results = safe_create_new_subgroup(position_group, "results")
    if "fit_parameters" in results_dict.keys():
        results.attrs["type"] = "fitted"
        for key, result in results_dict.items():
            results[key] = result
        results["measured_height"].attrs["units"] = "nm"
        results["extracted_positions"].attrs["units"] = "μm"
        results["extracted_heights"].attrs["units"] = "nm"
        results["adjusting_slope"].attrs["units"] = "nm/μm"
    else:
        results.attrs["type"] = "manual"
        for key, result in results_dict.items():
            results[key] = result
        results["measured_height"].attrs["units"] = "nm"
    return None


def update_dektak_hdf5(dektak_group):
    """
    Function to update an old version of a profilometry group to specs of newer versions.

    @param dektak_group:
    @return: True if group has been updated, False if group was already up to date
    """
    source_version = dektak_group.attrs["profil_writer"]

    if source_version == PROFIL_WRITER_VERSION:
        return False

    if "beta" in source_version:
        source_version = float(source_version.strip(" beta"))
    else:
        source_version = float(source_version)

    if source_version < 0.2:
        # Version 0.2 added manual vs fitted tags to results groups
        for position, position_group in dektak_group.items():
            results_group = position_group.get("results")
            if results_group:
                if "type" not in results_group.attrs:
                    results_group.attrs["type"] = "fitted"
        # end of patch

        # Update the version tag to the current version
        dektak_group.attrs["profil_writer"] = PROFIL_WRITER_VERSION
