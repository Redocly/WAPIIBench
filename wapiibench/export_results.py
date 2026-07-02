import json
import logging
import math
import os
import sys
import traceback

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from plotting_presets import *

# Switch between the evaluation and the validation results
DATASET, INPUT_DIR = "eval", "data/generated"
# DATASET, INPUT_DIR = "val", "validation_data/generated"
OUTPUT_DIR = "export/"

LOGGER_INFO = {
    'load_results': {
        'level': logging.WARNING
    },
    'results_to_pandas': {
        'level': logging.WARNING
    },
    'save_latex_table': {
        'level': logging.WARNING
    }
}


def _create_logger(scope):
    logging.basicConfig()
    logging.getLogger().setLevel(LOGGER_INFO[scope]['level'])
    logger = logging.getLogger(scope)
    return logger


def _load_json_files(path, experiment_id):
    logger = _create_logger(scope='load_results')
    try:
        with open(path, 'r') as file:
            file_dic = json.load(file)
        return file_dic
    except Exception:
        logger.debug(_full_stack())
        logger.warning(f"No results from {experiment_id}")
        return None


def _full_stack():
    exc = sys.exc_info()[0]
    stack = traceback.extract_stack()[:-1]  # last one would be full_stack()
    if exc is not None:  # i.e., an exception is present
        del stack[-1]  # remove call of full_stack, the printed exception
        # will contain the caught exception caller instead
    trc = "Traceback (most recent call last):\n"
    stackstr = trc + ''.join(traceback.format_list(stack))
    if exc is not None:
        stackstr += '  ' + traceback.format_exc().lstrip(trc)
    return stackstr


def _results_dict_to_pandas_frames(args, results_dict, experiment_id):
    def add_nested_metric():
        error_in_step = False
        for submetric in args.metrics[metric]:
            try:
                df_dict[f'{submetric}_{metric}'] = results_dict[top_level][metric].get(submetric, 0)
            except Exception:
                logger.debug(_full_stack())
                logger.warning(f"No metric for {experiment_id} {metric} {submetric}")
                error_in_step = True
        return error_in_step

    def add_individual_metrics():
        error_in_step = False
        try:
            df_dict[f'{metric}'] = results_dict[top_level][metric]
        except Exception:
            logger.debug(_full_stack())
            logger.warning(f"No metric for {experiment_id} {metric}")
            error_in_step = True
        return error_in_step

    df_dict = {}
    logger = _create_logger(scope='results_to_pandas')
    error_in_step = False
    top_level = 'statistics'
    for metric in args.metrics.keys():
        if len(args.metrics[metric]) > 0:
            error_in_step |= add_nested_metric()
        else:
            error_in_step |= add_individual_metrics()
    if error_in_step:
        logger.error(f"There is an error in the step: results_dict_to_pandas_frames for {experiment_id}")
    df_series = pd.Series(df_dict)
    return df_series


def _save_latex_table(args, df_all_results):
    logger = _create_logger(scope='save_latex_table')

    df_all_results.rename(index=args.row_renaming, columns=args.column_renaming, inplace=True)

    tabularx = False  # adapt depending on available horizontal space
    compact = 'compact' in args.filename
    if compact:
        NA_value = _replace_values_from_NA_list_compact(df_all_results)
    else:
        NA_value = _replace_values_from_NA_list_multi(df_all_results, args)

    latex_string = df_all_results.to_latex(float_format="%0.2f")
    with open(f"{OUTPUT_DIR}/{DATASET}_{args.filename}.tex", 'w') as tf:
        latex_string_split = latex_string.split("\n")
        for i, line in enumerate(latex_string_split):
            if not line:
                continue

            if tabularx and i == 0:
                line = line.replace("{lrr", "{Xrr")
                line = line.replace(r"\begin{tabular}", r"\begin{tabularx}{\linewidth}")

            elif i == 2:
                if compact:
                    line = _shorten_and_stack_column_headers(line)
                else:
                    # Remember to manually remove the \toprule if column headers are rotated
                    line = _shorten_and_rotate_column_headers(line)

            elif i > 3 and "&" in line:
                line = _bold_face_best_values(NA_value, line)
                line = _abbreviate_metric_names(line)
                line = line.replace(str(NA_value), "N/A")
                if not "(t)" in line and not "(e)" in line:
                    line = line.replace(".00", r"\phantom{.00}")
                if 'multi' in args.filename:
                    line = line.replace("0.", r"\phantom{00}0.")
                if args.delete_t_and_e:
                    line = line.replace(" (t)", "")
                    line = line.replace(" (e)", "")

            elif tabularx and i == len(latex_string_split) - 2:
                line = line.replace(r"\end{tabular}", r"\end{tabularx}")

            logger.debug(f"{i} {line}")
            tf.write(f"{line}\n")


def _bold_face_best_values(NA_value, line):
    # Highlight max/min values
    line = line.replace(r"\\", "")
    l_split = line.split("&")
    float_line = np.array(l_split[1:], dtype=float)
    if INVERSE_METRIC_MAP[l_split[0].strip()] in LOWER_IS_BETTER:
        extreme_value = float_line.min(initial=np.inf, where=float_line != NA_value)
    else:
        extreme_value = float_line.max(initial=-np.inf, where=float_line != NA_value)
    extreme_indices = np.where(float_line == extreme_value)[0]
    l_bold = [fr" \textbf{{{cell.strip()}}} " if i - 1 in extreme_indices else cell for i, cell in enumerate(l_split)]
    line = "&".join(l_bold)
    line += r"\\"
    return line


def _abbreviate_metric_names(line):
    line = line.replace("argument Jaccard", "arg. Jaccard")
    line = line.replace("argument value conditional accuracy", "arg. val. cond. acc.")
    # line = line.replace("Correct", "Corr.")
    # line = line.replace("Mean", "Avg.")
    # line = line.replace("arguments", "args.")
    # line = line.replace("argument", "arg.")
    # line = line.replace("Jaccard", "Jacc.")
    # line = line.replace("values", "vals.")
    # line = line.replace("value", "val.")
    # line = line.replace("accuracy", "acc.")
    return line


def _shorten_and_rotate_column_headers(line):
    line = line.replace("Invocation", "inv.")
    line = line.replace("Endpoint", "endp.")
    line = line.replace("vanilla", "UD")
    line = line.replace("constrained", "CD")
    line = line.removesuffix(r" \\")
    l_split = line.split(" & ")
    # adapt rotation depending on available space (e.g. 20 or 45)
    l_rotate = [fr"\makebox[20pt][l]{{\rotatebox{{45}}{{{cell}}}}}" for cell in l_split]
    line = " & ".join(l_rotate)
    line += r" \\"
    return line


def _shorten_and_stack_column_headers(line):
    num_invocation = line.count("Invocation")
    num_endpoint = line.count("Endpoint")
    line = line.replace("Invocation", "")
    line = line.replace("Endpoint", "")
    line = line.replace(" vanilla", "UD")
    line = line.replace(" constrained", "CD")
    return (
        f"& \\multicolumn{{{num_invocation}}}{{c}}{{Invocation}} & \\multicolumn{{{num_endpoint}}}{{c}}{{Endpoint}} \\\\\n"
        f"\\cmidrule(lr){{2-{num_invocation + 1}}}\\cmidrule(lr){{{num_invocation + 2}-{num_invocation + num_endpoint + 1}}}\n{line.lstrip()}")


def _replace_values_from_NA_list_compact(df_all_results):
    NA_value = np.inf
    for row, column in NA_list:
        if METRIC_MAP[row] not in df_all_results.index:
            continue
        if column == "invocation":
            df_all_results.loc[METRIC_MAP[row], 'Invocation vanilla'] = NA_value
            df_all_results.loc[METRIC_MAP[row], 'Invocation constrained'] = NA_value
        elif column == "endpoint":
            df_all_results.loc[METRIC_MAP[row], 'Endpoint vanilla'] = NA_value
            df_all_results.loc[METRIC_MAP[row], 'Endpoint constrained'] = NA_value
        elif column == "vanilla":
            df_all_results.loc[METRIC_MAP[row], 'Endpoint vanilla'] = NA_value
            df_all_results.loc[METRIC_MAP[row], 'Invocation vanilla'] = NA_value
    return NA_value


def _replace_values_from_NA_list_multi(df_all_results, args):
    NA_value = np.inf
    # The code below doesn't do anything meaningful. Since it cannot handle submetrics, let's skip it.
    # for metric, setup in NA_list:
    #     if METRIC_MAP[metric] not in df_all_results.index:
    #         continue
    #     elif setup == args.setups[0]:
    #         raise AssertionError
    return NA_value


def export(args):
    dict_with_pandas_frames = {}
    for model in args.models:
        for api in args.apis:
            for setup in args.setups:
                for setting in args.settings:
                    path_to_results = os.path.join(INPUT_DIR, model, api, setup, setting, "results.json")
                    experiment_id = f"{model}_{api}_{setup}_{setting}"
                    results_dict = _load_json_files(path_to_results, experiment_id)
                    if results_dict:
                        dict_with_pandas_frames[experiment_id] = \
                            _results_dict_to_pandas_frames(args, results_dict, experiment_id)
    df_all_results = pd.concat(dict_with_pandas_frames.values(), axis=1, keys=dict_with_pandas_frames.keys())
    for format in args.outputs:
        if format == 'tex':
            _save_latex_table(args, df_all_results)
        else:
            _plot_results(args, df_all_results, format)


def _plot_results(args, df, format):
    height = 6  # adapt depending on available vertical space
    scale = 0.81  # adapt depending on figsize and number of models
    legend = args.show_legend
    fig, axs = plt.subplots(
        nrows=len(args.apis), ncols=1, figsize=(5, height if legend else height * scale), sharex=False, dpi=600)
    labels = [f"1 − {args.row_renaming[label]}" if label in LOWER_IS_BETTER else args.row_renaming[label]
              for label in df.index]
    if args.delete_t_and_e:
        labels = [l.removesuffix(" (t)").removesuffix(" (e)") for l in labels]
    # Add a line break to long labels
    labels = _break_labels(labels, 40)
    labels = [label.replace("implementations", "imple-\nmentations", 1) for label in labels]  # optional
    ax = axs
    i = 0
    for api in args.apis:
        for model in args.models:
            ax = axs[i] if len(args.apis) > 1 else axs
            for setup in args.setups:
                for setting in args.settings:
                    key = f'{model}_{api}_{setup}_{setting}'
                    values = [
                        1 - value if label in LOWER_IS_BETTER else value for label, value in df.loc[:, key].items()]
                    ax.plot(labels, values, label=args.column_renaming[key], marker=MARKER_MAP[model], markersize=5,
                            linestyle='-')
            ax.set_ylim(ymin=0, ymax=1)  # this assumes that we are only plotting relative values, otherwise remove it
            # ax.set_xlabel("Metrics")  # should be self-explanatory
            ax.set_ylabel("Percentage")
            ax.set_title(API_MAP[api] if len(args.apis) > 1 else None)
            ax.set_xticks(range(len(labels)), labels=labels, rotation=30, ha='right')
            i += 1
    if legend:
        handles, labels = ax.get_legend_handles_labels()
        fig.legend(handles, labels, loc='upper right', bbox_to_anchor=(1.01, 1.01), ncol=2)

    fig.tight_layout(pad=0.01, rect=(0, 0, 1, scale if legend else 1))
    fig.savefig(f"{OUTPUT_DIR}/{DATASET}_{args.filename}{'' if legend else '_no_legend'}.{format}")
    plt.show()


def _break_labels(labels, threshold):
    return [l if len(l) <= threshold else
            l[:i] + "\n" + l[i + 1:] if (i := l.find(" ", len(l) // 3)) != -1
            else l for l in labels]


def export_settings_improvement(args):
    assert len(args.settings) == 2
    assert len(args.metrics) == 1
    metric = args.metrics[0]

    models = (*args.models, "Average", "Minimum", "Maximum")
    dfs = {setup: pd.DataFrame(index=models, columns=('S0', 'S1', 'Gain')) for setup in args.setups}

    buffer = [f"Metric: '{metric}'\n"]
    for api in args.apis:
        buffer.append(f"\tAPI: '{api}'\n")
        for index, model in enumerate(models):
            buffer.append(f"\t\tModel: '{model}'\n")
            for setup in args.setups:
                df = dfs[setup]
                if model == "Average":
                    df.iloc[index, :] = df.mean(skipna=True)  # infinite gains are excluded from the average
                    metric_0, metric_1, gain = df.iloc[index]

                elif model == "Minimum":
                    df.iloc[index, :] = df.min(skipna=True)
                    metric_0, metric_1, gain = df.iloc[index]

                elif model == "Maximum":
                    df.iloc[index, :] = df.max(skipna=True)
                    metric_0, metric_1, gain = df.iloc[index]

                else:
                    file_0 = os.path.join(INPUT_DIR, model, api, setup, args.settings[0], "results.json")
                    file_1 = os.path.join(INPUT_DIR, model, api, setup, args.settings[1], "results.json")

                    if not os.path.isfile(file_0) or not os.path.isfile(file_1):
                        print(f"Results not available:\t{file_0}\tand/or\t{file_1}", file=sys.stderr)
                        buffer.append(f"\t\t\t{setup}:\tN/A\n")
                        continue

                    with open(file_0, 'r') as file:
                        metric_0 = json.load(file)['statistics'][metric]

                    with open(file_1, 'r') as file:
                        metric_1 = json.load(file)['statistics'][metric]

                    # Uncomment to ignore models with zero values
                    # if metric_0 == 0 or metric_1 == 0:
                    #     df.iloc[index, :] = (np.nan, np.nan, np.nan)
                    #     buffer.append("\t\t\tskipped\n")
                    #     continue

                    gain = (metric_1 - metric_0) / metric_0 if metric_0 > 0 else np.nan
                    df.iloc[index, :] = (metric_0, metric_1, gain)

                buffer.append(f"\t\t\t{setup}:\t{gain:+.0%}\t({metric_0:.2f} -> {metric_1:.2f})\n")

        if 'print' in args.outputs:
            print("".join(buffer))

        for setup in args.setups:
            df = dfs[setup]
            df.rename(index=args.row_renaming, inplace=True)
            df.dropna(how='all', inplace=True)
            df.replace(np.nan, np.inf, inplace=True)
            for format in args.outputs:
                if format == 'tex':
                    # Remember to manually add a \midrule before the Average row
                    df.to_latex(
                        f"{OUTPUT_DIR}/{DATASET}_{args.filename}_{metric}_{api}_{setup}.tex",
                        header=[SETTING_MAP[args.settings[0]], SETTING_MAP[args.settings[1]], "Gain"],
                        formatters={
                            'S0': '{:.2f}'.format, 'S1': '{:.2f}'.format,
                            'Gain': lambda num: f"{num:+.0%}".replace("%", r"\%") if math.isfinite(num) else "N/A"
                        })
                elif format != 'print':
                    _create_improvement_plot(args, df, api, setup, format)


def _create_improvement_plot(args, df, api, setup, format):
    df.drop(index=["Average", "Minimum", "Maximum"], inplace=True)
    metric = args.metrics[0]
    # Position of bars
    x = np.arange(len(df.index))
    # Plot the stacked bars
    height = 5  # adapt depending on available vertical space
    scale = 0.95  # adapt depending on figsize
    legend = args.show_legend
    fig, ax = plt.subplots(figsize=(max(5, 0.4 * len(args.models)), height if legend else height * scale), dpi=600)
    # ax.scatter([], [], s=250, c='k', marker=r"$ {} $".format('x\%'), edgecolors='none', label='Gain')
    ax.bar(x, df.loc[:, 'S1'], label='S1', color='tab:olive')
    ax.bar(x, df.loc[:, 'S0'], label='S0', color='tab:blue')
    # Add text labels on top of the bars
    for i, inc in enumerate(df.loc[:, 'Gain']):
        ax.text(float(x[i]), df.iloc[i].loc['S1'] + 0.01, f"{inc:+.0%}", ha='center', va='bottom')
    ylabel = METRIC_MAP[metric]
    if args.delete_t_and_e:
        ylabel = ylabel.removesuffix(" (t)").removesuffix(" (e)")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    # Add a line break to long labels
    xticklabels = _break_labels(df.index, 25)
    ax.set_xticklabels(xticklabels, rotation=45, ha='right')
    ax.set_ylim(ymin=0, ymax=round(df.loc[:, ['S0', 'S1']].replace(np.inf, np.nan).max(axis=None) + 0.05, ndigits=1))
    if legend:
        # Add a legend
        handles, labels = ax.get_legend_handles_labels()
        handles.reverse()
        labels = (f"{SETTING_MAP[args.settings[0]]} baseline", f"Gain through {SETTING_MAP[args.settings[1]]}")
        fig.legend(handles, labels, loc='upper right', bbox_to_anchor=(1.0066, 1.0123), ncol=2)
    # ax.set_title(SETUP_MAP[setup], loc='left', x=0.01, y=0.92)  # optional
    fig.tight_layout(pad=0.01, rect=(0, 0, 1, scale if legend else 1))
    fig.savefig(
        f"{OUTPUT_DIR}/{DATASET}_{args.filename}_{metric}_{api}_{setup}{'' if legend else '_no_legend'}.{format}")
    # Show the plot
    plt.show()


def export_settings_comparison(args):
    assert len(args.apis) == 1
    assert len(args.setups) == 1
    assert len(args.metrics) == 1

    api = args.apis[0]
    setup = args.setups[0]
    metric = args.metrics[0]
    submetric = next((submetric for submetric in SUBMETRIC_MAP if metric.startswith(submetric)), None)

    # Collect all data in a dataframe
    df = pd.DataFrame(index=args.models, columns=args.settings)
    for model in args.models:
        for setting in args.settings:
            file_path = os.path.join(INPUT_DIR, model, api, setup, setting, "results.json")
            if not os.path.isfile(file_path):
                continue
            with open(file_path, 'r') as file:
                if submetric is None:
                    value = json.load(file)['statistics'][metric]
                else:
                    value = json.load(file)['statistics'][metric.removeprefix(submetric + "_")].get(submetric, 0)
            df.at[model, setting] = value

    df.rename(index=args.row_renaming, columns=args.column_renaming, inplace=True)

    for format in args.outputs:
        if format == 'tex':
            df.to_latex(f"{OUTPUT_DIR}/{DATASET}_{args.filename}_{metric}_{api}_{setup}.tex", float_format="%.2f")

        else:
            # Create grouped bar chart
            width = max(5, 0.4 * len(args.models))  # adapt depending on available horizontal space
            height = 5  # adapt depending on available vertical space
            legend = args.show_legend
            fig, ax = plt.subplots(figsize=(width, height), dpi=600)

            # Get the number of models and settings
            n_models = len(df.index)
            n_settings = len(df.columns)

            # Set the width of bars and positions
            bar_width = 0.8 / n_settings
            x = np.arange(n_models)

            # Create bars for each setting
            colors = ('tab:olive', 'tab:green', 'tab:cyan', 'tab:blue')
            for i, setting in enumerate(df.columns):
                positions = x + (i - n_settings / 2 + 0.5) * bar_width
                values = df.loc[:, setting]
                ax.bar(positions, values, bar_width, label=setting, color=colors[i])

            # Customize the plot
            ylabel = METRIC_MAP[metric]
            if args.delete_t_and_e:
                ylabel = ylabel.removesuffix(" (t)").removesuffix(" (e)")
            ax.set_ylabel(ylabel)
            ax.set_xticks(x)
            ax.set_xticklabels(df.index, rotation=45, ha='right')
            ax.set_title(SETUP_MAP[setup], loc='left', y=1.04)  # optional

            if legend:
                fig.legend(loc='upper right', ncol=n_settings)

            # Set y-axis to start from 0
            ax.set_ylim(bottom=0)

            fig.tight_layout(pad=0.01)
            fig.savefig(
                f"{OUTPUT_DIR}/{DATASET}_{args.filename}_{metric}_{api}_{setup}{'' if legend else '_no_legend'}.{format}")
            plt.show()


def _export_results():
    """Entry point for exporting the evaluation results as plots or tables."""

    # export(multi_model_plot('invocation', 'vanilla'))
    # export(multi_model_plot('endpoint', 'vanilla'))
    # export(multi_model_plot('invocation', 'constrained'))
    # export(multi_model_plot('endpoint', 'constrained'))

    # export(multi_model_table('invocation', 'vanilla'))
    # export(multi_model_table('endpoint', 'vanilla'))
    # export(multi_model_table('invocation', 'rag'))
    # export(multi_model_table('endpoint', 'rag'))
    # export(multi_model_table('invocation', 'constrained'))
    # export(multi_model_table('endpoint', 'constrained'))
    # export(multi_model_table('invocation', 'constrained-rag'))
    # export(multi_model_table('endpoint', 'constrained-rag'))

    # export(multi_model_submetric_table('invocation', 'vanilla'))
    # export(multi_model_submetric_table('endpoint', 'vanilla'))
    # export(multi_model_submetric_table('invocation', 'constrained'))
    # export(multi_model_submetric_table('endpoint', 'constrained'))

    # export(single_model_table('starcoder2-15b', 'invocation', 'vanilla'))
    # export(single_model_table('starcoder2-15b', 'endpoint', 'vanilla'))
    # export(single_model_table('starcoder2-15b', 'invocation', 'constrained'))
    # export(single_model_table('starcoder2-15b', 'endpoint', 'constrained'))
    # export(single_model_table('gpt-4o', 'invocation', 'vanilla'))
    # export(single_model_table('gpt-4o', 'endpoint', 'vanilla'))

    # export_settings_improvement(settings_improvement('samples_correct_wrt_total', 'invocation'))
    # export_settings_improvement(settings_improvement('samples_correct_wrt_total', 'endpoint'))
    # export_settings_improvement(settings_improvement('endpoints_illegal_wrt_total', 'invocation'))
    # export_settings_improvement(settings_improvement('samples_illegal_wrt_total', 'endpoint'))
    # export_settings_improvement(settings_improvement('samples_executable_wrt_total', 'invocation'))
    # export_settings_improvement(settings_improvement('samples_executable_wrt_total', 'endpoint'))

    export_settings_comparison(settings_comparison('samples_correct_wrt_total', 'invocation'))
    export_settings_comparison(settings_comparison('samples_correct_wrt_total', 'endpoint'))
    export_settings_comparison(settings_comparison('endpoints_illegal_wrt_total', 'invocation'))
    export_settings_comparison(settings_comparison('samples_illegal_wrt_total', 'endpoint'))
    # export_settings_comparison(settings_comparison('samples_executable_wrt_total', 'invocation'))
    # export_settings_comparison(settings_comparison('samples_executable_wrt_total', 'endpoint'))


if __name__ == '__main__':
    os.chdir(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))
    sys.path.append(os.getcwd())

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    _export_results()
