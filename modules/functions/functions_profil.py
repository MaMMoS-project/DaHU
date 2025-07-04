from scipy.optimize import least_squares, curve_fit
from scipy.signal import savgol_filter
import plotly.graph_objs as go
from sklearn.linear_model import RANSACRegressor, LinearRegression
from sklearn.preprocessing import PolynomialFeatures
from sklearn.linear_model import HuberRegressor
from functions_shared import *
from modules.hdf5_compilers.hdf5compile_profil import write_dektak_results_to_hdf5


def profil_conditions(hdf5_path, *args, **kwargs):
    if hdf5_path is None:
        return False
    if not h5py.is_hdf5(hdf5_path):
        return False
    with h5py.File(hdf5_path, "r") as hdf5_file:
        dataset_list = get_hdf5_datasets(hdf5_file, dataset_type="profil")
        if len(dataset_list) == 0:
            return False
    return True


def profil_get_measurement_from_hdf5(profil_group, target_x, target_y):
    for position, position_group in profil_group.items():
        instrument_group = position_group.get("instrument")
        if instrument_group["x_pos"][()] == target_x and instrument_group["y_pos"][()] == target_y:
            measurement_group = position_group.get("measurement")

            distance_array = measurement_group["distance"][()]
            profile_array = measurement_group["profile"][()]

            measurement_dataframe = pd.DataFrame({"distance_(um)": distance_array, "total_profile_(nm)": profile_array})

            return measurement_dataframe


def profil_get_results_from_hdf5(profil_group, target_x, target_y):
    data_dict = {}

    for position, position_group in profil_group.items():
        instrument_group = position_group.get("instrument")
        if instrument_group["x_pos"][()] == target_x and instrument_group["y_pos"][()] == target_y:
            results_group = position_group.get("results")
            if results_group is None:
                return None
            for value, value_group in results_group.items():
                data_dict[value] = value_group[()]
            data_dict["type"] = results_group.attrs["type"]

    return data_dict


def multi_step_function(x, *params):
    # Generate function with multiple steps
    # Even indices are the x positions of the steps,
    # Odd indices are the y values after each step.

    y = np.full_like(x, params[1])
    for i in range(0, len(params) - 2, 2):
        x0 = params[i]
        y1 = params[i + 3]
        y = np.where(x >= x0, y1, y)
    return y

def parabola(x, a, b, c):
    return a * x**2 + b * x + c

def generate_parameters(height, x0, n_steps):
    guess = []
    length = 4000 / (2*n_steps) # Length of a step
    for n in range(n_steps+1):
        guess.append(x0+2*length*n) # Step up position
        guess.append(height) # Step up height
        guess.append(x0+length+2*length*n) # Step down position
        guess.append(0) # Step down height
    return guess


def extract_fit(fitted_params):
    # Separate fitted parameters
    fit_position_list = []
    fit_height_list = []
    for position, height in pairwise(fitted_params):
        fit_position_list.append(np.round(position, 1))
        fit_height_list.append(np.round(height, 1))

    # From fitted parameters, calculate positions
    position_list = []
    for n in range(len(fit_position_list) - 1):
        a = fit_position_list[n]
        b = fit_position_list[n + 1]
        position_list.append(np.round(np.abs(b - (b - a) / 2), 1))
    position_list.pop()

    # From fitted parameters, calculate heights
    height_list = []
    for n in range(len(fit_height_list) - 1):
        a = fit_height_list[n]
        b = fit_height_list[n + 1]
        height_list.append(np.round(np.abs(a - b), 1))
    height_list.pop()

    return position_list, height_list


def residuals(params, x, y):
    # Loss function for fitting
    return y - multi_step_function(x, *params)


def profil_measurement_dataframe_treat(df, coefficients=None, smoothing=True):
    # Calculate and remove linear component from profile with step point linear fit
    if coefficients is None:
        coefficients = curve_fit(parabola, df["distance_(um)"], df["total_profile_(nm)"])[0]

    df["adjusted_profile_(nm)"] = df["total_profile_(nm)"] - parabola(df["distance_(um)"], coefficients[0], coefficients[1], coefficients[2])
    if smoothing:
        df["adjusted_profile_(nm)"] = savgol_filter(df["adjusted_profile_(nm)"], 100, 0)

    return coefficients, df


def profil_measurement_dataframe_fit_steps(df, n_steps, x0_guess):
    results_dict = {}

    if "adjusted_profile_(nm)" not in df.columns:
        coefficients, df = profil_measurement_dataframe_treat(df, smoothing=True)
        results_dict["adjusting_slope"] = coefficients

    # Detect the position of the first step of the measurement
    df = derivate_dataframe(df, column="adjusted_profile_(nm)")
    df_head = df.loc[(df["distance_(um)"] > x0_guess*0.7) & (df["distance_(um)"] < x0_guess*1.3)]
    max_index = np.abs(df_head["derivative"]).idxmax()
    x0 = df_head.loc[max_index, "distance_(um)"]

    distance_array = df["distance_(um)"].to_numpy()
    profile_array = df["adjusted_profile_(nm)"].to_numpy()

    guess = generate_parameters(height = 1, x0 = x0, n_steps = n_steps)

    result = least_squares(residuals, guess, jac="2-point", args=(distance_array, profile_array), loss="soft_l1")
    fitted_params = result.x

    position_list, height_list = extract_fit(fitted_params)

    results_dict["fit_parameters"] = fitted_params
    results_dict["extracted_positions"] = position_list
    results_dict["extracted_heights"] = height_list
    results_dict["measured_height"] = np.mean(height_list).round()

    # ransac = RANSACRegressor(LinearRegression(), residual_threshold=None)
    # ransac.fit(position_list, height_list)
    # top_coefficients = np.append(np.array(ransac.estimator_.intercept_), ransac.estimator_.coef_)

    return results_dict

def profil_spot_fit_steps(position_group, nb_steps, x0):
    measurement_group = position_group.get("measurement")

    distance_array = measurement_group["distance"][()]
    profile_array = measurement_group["profile"][()]

    measurement_dataframe = pd.DataFrame({"distance_(um)": distance_array, "total_profile_(nm)": profile_array})

    results_dict = profil_measurement_dataframe_fit_steps(measurement_dataframe, nb_steps, x0)

    return results_dict


def profil_make_results_dataframe_from_hdf5(profil_group):
    data_dict_list = []

    for position, position_group in profil_group.items():
        instrument_group = position_group.get("instrument")
        # Exclude spots outside the wafer
        if np.abs(instrument_group["x_pos"][()]) + np.abs(instrument_group["y_pos"][()]) <= 60:

            results_group = position_group.get("results")

            data_dict = {"x_pos (mm)": instrument_group["x_pos"][()],
                         "y_pos (mm)": instrument_group["y_pos"][()],
                         "ignored": position_group.attrs["ignored"]}

            if results_group is not None:
                for value, value_group in results_group.items():
                    if "units" in value_group.attrs:
                        units = value_group.attrs["units"]
                    else:
                        units = "arb"
                    data_dict[f"{value}_({units})"] = value_group[()]

            data_dict_list.append(data_dict)

    result_dataframe = pd.DataFrame(data_dict_list)

    return result_dataframe


def profil_plot_total_profile_from_dataframe(fig, df, adjusting_slope = None, position=(1,1)):
    # First plot for raw measurement and linear component
    fig.update_xaxes(title_text="Distance_(um)", row=1, col=1)
    fig.update_yaxes(title_text="Profile_(nm)", row=1, col=1)

    # Add measurement trace
    fig.add_trace(
        go.Scatter(
            x=df["distance_(um)"],
            y=df["total_profile_(nm)"],
            mode="lines",
            line=dict(color="SlateBlue", width=3),
        ), row = position[0], col = position[1]
    )

    # If a slope is specified, plot the slope
    if adjusting_slope is not None:
        fig.add_trace(
            go.Scatter(
                x=df["distance_(um)"],
                y=parabola(df["distance_(um)"], adjusting_slope[0], adjusting_slope[1], adjusting_slope[2]),
                mode="lines",
                line=dict(color="Crimson", width=2),
            ), row = position[0], col = position[1]
        )

    return fig


def profil_plot_adjusted_profile_from_dataframe(fig, df, fit_parameters = None, position=(2,1)):
    # Second plot for adjusted profile and fits
    fig.update_xaxes(title_text="Distance_(um)", row=2, col=1)
    fig.update_yaxes(title_text="Thickness_(nm)", row=2, col=1)

    # Plot the profile after linear adjustment
    fig.add_trace(
        go.Scatter(
            x=df["distance_(um)"],
            y=df["adjusted_profile_(nm)"],
            mode="lines",
            line=dict(color="SlateBlue", width=3),
        ), row=position[0], col=position[1]
    )

    # If fit parameters are specified, plot the fitted steps
    if fit_parameters is not None:
        fig.add_trace(
            go.Scatter(
                x=df["distance_(um)"],
                y=multi_step_function(df["distance_(um)"], *fit_parameters),
                mode="lines",
                line=dict(color="Crimson", width=2),
            ), row=position[0], col=position[1]
        )

    return fig


def profil_plot_measured_heights_from_dict(fig, results_dict, position=(3,1)):
    position_list = results_dict["extracted_positions"]
    height_list = results_dict["extracted_heights"]
    measured_height = results_dict["measured_height"]

    # Third plot
    fig.update_xaxes(title_text="Distance_(um)", row=2, col=1)
    fig.update_yaxes(title_text="Thickness_(nm)", row=2, col=1)

    # Scattered heights
    fig.add_trace(
        go.Scatter(x=position_list, y=height_list,
                   mode="markers",
                   # name="Measured thickness",
                   line=dict(color="SlateBlue ", width=3)),
        row=position[0], col=position[1]
    )

    # Mean line
    fig.add_hline(y=measured_height, line=dict(color="Crimson", width=2), row=position[0], col=position[1])

    return fig
























"""
ARCHIVE FOR POLYNOMIAL FIT, MIGHT COME BACK TO IT SOMEDAY
"""

# def profil_measurement_dataframe_fit_poly(df, est_height, degree = 3):
#     results_dict = {}
#
#     if "adjusted_profile_(nm)" not in df.columns:
#         slope, df = profil_measurement_dataframe_treat(df, smoothing=True)
#         results_dict["adjusting_slope"] = slope
#
#     distance_array = df["distance_(um)"].to_numpy()
#     profile_array = df["adjusted_profile_(nm)"].to_numpy()
#
#     # Split the data horizontally in two
#     top_mask = profile_array > est_height / 2
#     x_top = distance_array[top_mask].reshape(-1, 1)
#     y_top = profile_array[top_mask]
#
#     bottom_mask = profile_array <= est_height / 2
#     x_bottom = distance_array[bottom_mask].reshape(-1, 1)
#     y_bottom = profile_array[bottom_mask]
#
#     poly = PolynomialFeatures(degree=degree, include_bias=False)
#     x_poly_top = poly.fit_transform(x_top.reshape(-1, 1))
#     x_poly_bottom = poly.fit_transform(x_bottom.reshape(-1, 1))
#
#     # Fit for the top line
#     ransac_top = RANSACRegressor(LinearRegression(), residual_threshold=None)
#     ransac_top.fit(x_poly_top, y_top)
#     top_coefficients = np.append(np.array(ransac_top.estimator_.intercept_), ransac_top.estimator_.coef_)
#
#     # Fit for the bottom line
#     ransac_bottom = RANSACRegressor(LinearRegression(), residual_threshold=None)
#     ransac_bottom.fit(x_poly_bottom, y_bottom)
#     bottom_coefficients = np.append(np.array(ransac_bottom.estimator_.intercept_), ransac_bottom.estimator_.coef_)
#
#     results_dict["Top fit coefficients"] = top_coefficients
#     results_dict["Bottom fit coefficients"] = bottom_coefficients
#
#     difference_coefficients = top_coefficients - bottom_coefficients
#     measured_height = np.mean(calc_poly(difference_coefficients, distance_array[-1]))
#
#     results_dict["measured_height"] = measured_height
#
#     return results_dict
#
#
# def profil_batch_fit_poly(hdf5_path, est_height, degree=3):
#     if not check_for_profil(hdf5_path):
#         raise KeyError("Profilometry not found in file. Please check your file")
#
#     with h5py.File(hdf5_path, mode="a") as hdf5_file:
#         profil_group = hdf5_file["/profil"]
#         for position, position_group in profil_group.items():
#             measurement_group = position_group.get("measurement")
#
#             distance_array = measurement_group["distance"][()]
#             profile_array = measurement_group["profile"][()]
#
#             measurement_dataframe = pd.DataFrame({"distance_(um)": distance_array, "total_profile_(nm)": profile_array})
#
#             results_dict = profil_measurement_dataframe_fit_poly(measurement_dataframe, est_height, degree)
#
#             if "results" in position_group:
#                 del position_group["results"]
#
#             results = position_group.create_group("results")
#             try:
#                 for key, result in results_dict.items():
#                     results[key] = result
#                 results["measured_height"].attrs["units"] = "nm"
#             except KeyError:
#                 raise KeyError("Given results dictionary not compatible with current version of this function."
#                                "Check compatibility with fit function")
#
#     return True

# def profil_plot_poly_measurement_from_dataframe(df, results_dict={}):
#     slope, df = profil_measurement_dataframe_treat(df)
#
#     # Plot the data
#     fig = make_subplots(
#         rows=2,
#         cols=1,
#         row_heights=[0.6, 0.4],
#         subplot_titles=("Fitted data", "Measured thicknesses"),
#         shared_xaxes=True,
#         vertical_spacing=0.1
#     )
#
#
#     # First plot for raw measurement and linear component
#     fig.update_xaxes(title_text="distance_(um)", row=1, col=1)
#     fig.update_yaxes(title_text="profile_(nm)", row=1, col=1)
#
#     fig.add_trace(
#         go.Scatter(
#             x=df["distance_(um)"],
#             y=df["total_profile_(nm)"],
#             mode="lines",
#             line=dict(color="SlateBlue", width=2),
#         ), row = 1, col = 1
#     )
#
#     if "adjusting_slope" in results_dict.keys():
#         fig.add_trace(
#             go.Scatter(
#                 x=df["distance_(um)"],
#                 y=df["distance_(um)"] * results_dict["adjusting_slope"] + df.iat[0,1],
#                 mode="lines",
#                 line=dict(color="Crimson", width=2),
#             ), row = 1, col = 1
#         )
#
#     # Second plot for adjusted profile and fits
#     fig.update_xaxes(title_text="distance_(um)", row=2, col=1)
#     fig.update_yaxes(title_text="Thickness_(nm)", row=2, col=1)
#
#     fig.add_trace(
#         go.Scatter(
#             x=df["distance_(um)"],
#             y=df["adjusted_profile_(nm)"],
#             mode="lines",
#             line=dict(color="SlateBlue", width=2),
#         ), row = 2, col = 1
#     )
#
#     if "Top fit coefficients" in results_dict.keys():
#         polynomial = calc_poly(results_dict["Top fit coefficients"], x_end=df["distance_(um)"].iloc[-1], x_start=0, x_step=df["distance_(um)"].iloc[-1]/len(df["distance_(um)"]))
#         fig.add_trace(
#             go.Scatter(
#                 x=df["distance_(um)"],
#                 y=polynomial,
#                 mode="lines",
#                 line=dict(color="Crimson", width=2),
#             ), row = 2, col = 1
#         )
#
#     if "Bottom fit coefficients" in results_dict.keys():
#         polynomial = calc_poly(results_dict["Bottom fit coefficients"], x_end=df["distance_(um)"].iloc[-1], x_start=0,
#                                x_step=df["distance_(um)"].iloc[-1] / len(df["distance_(um)"]))
#         fig.add_trace(
#             go.Scatter(
#                 x=df["distance_(um)"],
#                 y=polynomial,
#                 mode="lines",
#                 line=dict(color="Crimson", width=2),
#             ), row=2, col=1
#         )
#
#
#     fig.update_layout(plot_layout(""))
#
#     return fig



