#!/usr/bin/env python3
import subprocess
import json
import argparse
import os
import sys
from flask import Flask, Response

app = Flask(__name__)


# ------------------------------
# Helpers to call whmapi1 & uapi
# ------------------------------

def run_whmapi(args):
    """
    Helper to run a WHM API 1 command and return parsed JSON.
    Only meant to be run as root on the server.
    """
    cmd = ['whmapi1', '--output=json'] + args

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        app.logger.error(
            f"WHMAPI command failed ({' '.join(cmd)}): rc={result.returncode}, stderr={result.stderr}"
        )
        raise RuntimeError(f"WHMAPI command failed: {result.stderr}")

    if not result.stdout:
        app.logger.error(f"WHMAPI command returned empty stdout for: {' '.join(cmd)}")
        raise RuntimeError("Empty stdout from WHMAPI")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        app.logger.error(
            f"Failed to parse JSON from WHMAPI ({' '.join(cmd)}): {e} | stdout={result.stdout[:500]}"
        )
        raise


def run_uapi_for_user(cpanel_user, args):
    """
    Helper to run a UAPI command for a specific cPanel user and return parsed JSON.
    Always uses --user=<cpanel_user>.
    """
    cmd = ['uapi', '--output=json', f'--user={cpanel_user}']
    cmd.extend(args)

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        app.logger.error(
            f"UAPI command failed ({' '.join(cmd)}): rc={result.returncode}, stderr={result.stderr}"
        )
        raise RuntimeError(f"UAPI command failed for user {cpanel_user}: {result.stderr}")

    if not result.stdout:
        app.logger.error(f"UAPI command returned empty stdout for user {cpanel_user}: {' '.join(cmd)}")
        raise RuntimeError(f"Empty stdout from UAPI for user {cpanel_user}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        app.logger.error(
            f"Failed to parse JSON from UAPI for user {cpanel_user} ({' '.join(cmd)}): "
            f"{e} | stdout={result.stdout[:500]}"
        )
        raise


# ------------------------------
# Server-wide: fetch all users
# ------------------------------

def fetch_all_cpanel_users():
    """
    Uses WHM API 1 listaccts to get all cPanel account usernames.
    """
    data = run_whmapi(['listaccts'])
    # Shape: { "metadata": {...}, "data": { "acct": [ { "user": "xxx", ... }, ... ] } }
    accounts = data.get('data', {}).get('acct', [])
    users = [acct.get('user') for acct in accounts if acct.get('user')]
    if not users:
        app.logger.warning("WHM listaccts returned no users")
    return users


# ------------------------------
# Per-user UAPI fetchers
# ------------------------------

def fetch_cpanel_metrics(user):
    """
    Fetches general metrics data from cPanel StatsBar for a given user.
    """
    data = run_uapi_for_user(user, [
        'StatsBar', 'get_stats',
        'display=bandwidthusage|diskusage|addondomains|autoresponders|cachedlistdiskusage|cachedmysqldiskusage'
        '|cpanelversion|emailaccounts|emailfilters|emailforwarders|filesusage|ftpaccounts|hostingpackage|hostname'
        '|kernelversion|machinetype|operatingsystem|mailinglists|mysqldatabases|mysqldiskusage|mysqlversion'
        '|parkeddomains|perlversion|phpversion|shorthostname|sqldatabases|subdomains|cachedpostgresdiskusage'
        '|postgresqldatabases|postgresdiskusage'
    ])

    result = data.get('result', {})
    metrics = result.get('data', [])
    if not metrics:
        app.logger.warning(f"StatsBar returned no metrics data for user {user}")
    return metrics


def construct_labels(user, metrics):
    """
    Constructs the labels string from metric items, including user and IP.
    """
    user_ip_data = run_uapi_for_user(user, ['Variables', 'get_user_information'])

    user_info = user_ip_data.get('result', {}).get('data', {})
    ip = user_info.get('ip', 'unknown')

    labels_dict = {}
    for item in metrics:
        name = item.get('name')
        value = item.get('value')
        if (
            name
            and value is not None
            and isinstance(value, str)
            and name not in ['diskusage', 'bandwidthusage']
        ):
            labels_dict[name] = value.replace('"', '\\"')

    labels_dict['user'] = user
    labels_dict['ip'] = ip

    return ",".join(f'{key}="{value}"' for key, value in labels_dict.items())


def fetch_resource_usage_metrics(user):
    """
    Fetches resource usage (CPU, MEM, processes, etc.) metrics data from cPanel for a given user.
    """
    try:
        resource_data = run_uapi_for_user(user, ['ResourceUsage', 'get_usages'])
    except Exception as e:
        app.logger.error(f"Error executing uapi for Resource Usage metrics for user {user}: {e}")
        return []

    result = resource_data.get('result', {})
    if result.get('status') == 0 and result.get('errors'):
        error_message = result['errors'][0]
        app.logger.error(f"Error fetching resource usage metrics for user {user}: {error_message}")
        return []

    resource_metrics = result.get('data')
    if resource_metrics is None:
        app.logger.warning(f"No Resource usage data found for user {user}.")
        return []

    return resource_metrics


def format_resource_usage_metrics(resource_metrics, labels_string):
    """
    Formats resource usage metrics and returns lines in Prometheus text format.
    """
    formatted_metrics_output = []
    for metric in resource_metrics:
        metric_id = metric.get('id')
        usage = metric.get('usage')
        maximum = metric.get('maximum')

        if metric_id not in ['lvecpu', 'lveep', 'lvememphy', 'lveiops', 'lveio', 'lvenproc']:
            continue

        try:
            metric_value = float(usage)
        except (TypeError, ValueError):
            continue

        metric_name = f"cpanel_{metric_id[3:]}"  # strip 'lve'
        # CPU & MEM percentage
        if metric_id in ['lvecpu', 'lvememphy'] and maximum:
            try:
                metric_percent = round((metric_value / float(maximum)) * 100, 2)
                formatted_metrics_output.append(
                    f"{metric_name}_percent{{{labels_string}}} {metric_percent}"
                )
            except (TypeError, ValueError):
                pass

        formatted_metrics_output.append(
            f"{metric_name}{{{labels_string}}} {metric_value}"
        )

    return formatted_metrics_output


def fetch_mysql_db_metrics(user):
    """Fetches MySQL database metrics data from cPanel for a given user."""
    try:
        mysql_db_data = run_uapi_for_user(user, ['Mysql', 'list_databases'])
    except Exception as e:
        app.logger.error(f"Error executing uapi for MySQL database metrics for user {user}: {e}")
        return []

    result = mysql_db_data.get('result', {})
    if result.get('status') == 0 and result.get('errors'):
        error_message = result['errors'][0]
        if "You do not have the feature" in error_message:
            app.logger.warning(f"MySQL feature unavailable for user {user}: {error_message}")
            return []

    mysql_db_metrics = result.get('data')
    if mysql_db_metrics is None:
        app.logger.warning(f"No MySQL databases found or the feature is disabled for user {user}.")
        return []

    return mysql_db_metrics


def format_mysql_db_metrics(mysql_db_metrics, labels_string):
    """Formats MySQL database metrics to Prometheus text lines."""
    mysql_db_metrics_output = []
    for db in mysql_db_metrics:
        database_name = db.get('database')
        disk_usage_bytes = db.get('disk_usage')

        if database_name is None or disk_usage_bytes is None:
            continue

        try:
            disk_usage_bytes = float(disk_usage_bytes)
        except (TypeError, ValueError):
            continue

        formatted_metric = (
            f"cpanel_mysql_db_disk_usage{{db=\"{database_name}\",{labels_string}}} "
            f"{disk_usage_bytes}"
        )
        mysql_db_metrics_output.append(formatted_metric)

    return mysql_db_metrics_output


def fetch_postgres_db_metrics(user):
    """Fetches PostgreSQL database metrics data from cPanel for a given user."""
    try:
        pg_data = run_uapi_for_user(user, ['Postgresql', 'list_databases'])
    except Exception as e:
        app.logger.error(f"Error executing uapi for PostgreSQL database metrics for user {user}: {e}")
        return []

    result = pg_data.get('result', {})
    if result.get('status') == 0 and result.get('errors'):
        error_message = result['errors'][0]
        if "You do not have the feature" in error_message:
            app.logger.warning(f"PostgreSQL feature unavailable for user {user}: {error_message}")
            return []

    pg_metrics = result.get('data')
    if pg_metrics is None:
        app.logger.warning(f"No PostgreSQL databases found or the feature is disabled for user {user}.")
        return []

    return pg_metrics


def format_postgres_db_metrics(pg_metrics, labels_string):
    """Formats PostgreSQL database metrics to Prometheus text lines."""
    pg_metrics_output = []
    for db in pg_metrics:
        database_name = db.get('database')
        disk_usage_bytes = db.get('disk_usage')

        if database_name is None or disk_usage_bytes is None:
            continue

        try:
            disk_usage_bytes = float(disk_usage_bytes)
        except (TypeError, ValueError):
            continue

        formatted_metric = (
            f"cpanel_postgres_db_disk_usage{{db=\"{database_name}\",{labels_string}}} "
            f"{disk_usage_bytes}"
        )
        pg_metrics_output.append(formatted_metric)

    return pg_metrics_output


def fetch_email_metrics(user):
    """Fetches email metrics data from cPanel for a given user."""
    try:
        email_data = run_uapi_for_user(user, ['Email', 'list_pops_with_disk'])
    except Exception as e:
        app.logger.error(f"Error executing uapi for Email accounts metrics for user {user}: {e}")
        return []

    result = email_data.get('result', {})
    if result.get('status') == 0 and result.get('errors'):
        error_message = result['errors'][0]
        if "You do not have the feature" in error_message:
            app.logger.warning(f"Email accounts feature unavailable for user {user}: {error_message}")
            return []

    email_metrics = result.get('data')
    if email_metrics is None:
        app.logger.warning(f"No Email accounts found or the feature is disabled for user {user}.")
        return []

    return email_metrics


def format_email_metrics(email_metrics, labels_string):
    """Formats email metrics to Prometheus text lines."""
    email_metrics_output = []
    for email_info in email_metrics:
        email = email_info.get('email')
        disk_used_value = email_info.get('_diskused')

        if email is None or disk_used_value is None:
            continue

        try:
            disk_used_bytes = int(float(disk_used_value))
        except (TypeError, ValueError):
            continue

        formatted_metric = (
            f"cpanel_email_disk_usage{{email=\"{email}\",{labels_string}}} "
            f"{disk_used_bytes}"
        )
        email_metrics_output.append(formatted_metric)

    return email_metrics_output


def fetch_ftp_metrics(user):
    """Fetches FTP metrics data from cPanel for a given user."""
    try:
        ftp_data = run_uapi_for_user(user, ['Ftp', 'list_ftp_with_disk'])
    except Exception as e:
        app.logger.error(f"Error executing uapi for FTP accounts metrics for user {user}: {e}")
        return []

    result = ftp_data.get('result', {})
    if result.get('status') == 0 and result.get('errors'):
        error_message = result['errors'][0]
        if "You do not have the feature" in error_message:
            app.logger.warning(f"FTP accounts feature unavailable for user {user}: {error_message}")
            return []

    ftp_metrics = result.get('data')
    if ftp_metrics is None:
        app.logger.warning(f"No FTP accounts found or the feature is disabled for user {user}.")
        return []

    return ftp_metrics


def format_ftp_metrics(ftp_metrics, labels_string):
    """Formats FTP metrics to Prometheus text lines."""
    ftp_metrics_output = []
    for ftp_info in ftp_metrics:
        ftp = ftp_info.get('login')
        disk_used_value = ftp_info.get('_diskused')

        if ftp is None or disk_used_value is None:
            continue

        try:
            disk_used_mb = float(disk_used_value)
            disk_used_bytes = int(disk_used_mb * 1024 * 1024)
        except (TypeError, ValueError):
            continue

        formatted_metric = (
            f"cpanel_ftp_account_disk_usage{{ftp_account=\"{ftp}\",{labels_string}}} "
            f"{disk_used_bytes}"
        )
        ftp_metrics_output.append(formatted_metric)

    return ftp_metrics_output


# ------------------------------
# Flask: /metrics endpoint
# ------------------------------

@app.route('/metrics')
def metrics():
    """
    Generates and returns Prometheus metrics for ALL cPanel users on this server.
    """
    try:
        all_users = fetch_all_cpanel_users()
        global_output_lines = []

        for user in all_users:
            try:
                metrics_list = fetch_cpanel_metrics(user)
                if not metrics_list:
                    continue

                labels_string = construct_labels(user, metrics_list)

                numeric_metrics_output = []

                for item in metrics_list:
                    name = item.get('name')
                    if not name:
                        continue

                    metric_name = 'cpanel_' + name
                    metric_value = item.get('_count')
                    if metric_value is None:
                        metric_value = item.get('value')

                    # Normalize metric_value to float or skip if non-numeric
                    if isinstance(metric_value, str):
                        s = metric_value.strip()
                        if s.replace('.', '', 1).isdigit():
                            metric_value = float(s)
                        else:
                            # non-numeric string, skip this metric
                            continue
                    elif isinstance(metric_value, (int, float)):
                        metric_value = float(metric_value)
                    else:
                        # unknown type, skip
                        continue

                    # Convert units to bytes where appropriate
                    if name not in ['mysqldiskusage', 'cachedmysqldiskusage',
                                    'postgresdiskusage', 'cachedpostgresdiskusage']:
                        units = item.get('units')
                        if units == "GB":
                            metric_value *= 1024 ** 3
                        elif units == "MB":
                            metric_value *= 1024 ** 2

                    # diskusage / filesusage: also expose free values and percentages
                    if name in ['diskusage', 'filesusage']:
                        max_value = item.get('_max')
                        percent = float(item.get('percent', 0))
                        percent_free = 100 - percent

                        if max_value and isinstance(max_value, str) and max_value.lower() != "unlimited":
                            try:
                                if name == 'diskusage':
                                    # diskusage: max in MB -> bytes
                                    max_value_bytes = float(max_value) * 1024 * 1024
                                else:
                                    # filesusage: max as count
                                    max_value_bytes = float(max_value)

                                free_value = max_value_bytes - metric_value

                                numeric_metrics_output.append(
                                    f"cpanel_free_{name}{{{labels_string}}} {free_value}"
                                )
                                numeric_metrics_output.append(
                                    f"cpanel_free_{name}_percent{{{labels_string}}} {percent_free}"
                                )
                                numeric_metrics_output.append(
                                    f"cpanel_{name}_percent{{{labels_string}}} {percent}"
                                )
                            except (TypeError, ValueError):
                                pass

                    formatted_metric = f"{metric_name}{{{labels_string}}} {metric_value}"
                    numeric_metrics_output.append(formatted_metric)

                # Static info metric per user
                info_metric = f'cpanel_info{{{labels_string}}} 1'
                numeric_metrics_output.append(info_metric)

                # Add resource, DB, email, FTP metrics for this user
                resource_metrics = fetch_resource_usage_metrics(user)
                resource_metrics_output = format_resource_usage_metrics(resource_metrics, labels_string)

                mysql_db_metrics = fetch_mysql_db_metrics(user)
                mysql_db_metrics_output = format_mysql_db_metrics(mysql_db_metrics, labels_string)

                pg_metrics = fetch_postgres_db_metrics(user)
                pg_metrics_output = format_postgres_db_metrics(pg_metrics, labels_string)

                email_metrics = fetch_email_metrics(user)
                email_metrics_output = format_email_metrics(email_metrics, labels_string)

                ftp_metrics = fetch_ftp_metrics(user)
                ftp_metrics_output = format_ftp_metrics(ftp_metrics, labels_string)

                # Aggregate all lines for this user into global output
                global_output_lines.extend(numeric_metrics_output)
                global_output_lines.extend(resource_metrics_output)
                global_output_lines.extend(mysql_db_metrics_output)
                global_output_lines.extend(pg_metrics_output)
                global_output_lines.extend(email_metrics_output)
                global_output_lines.extend(ftp_metrics_output)

            except Exception as user_err:
                app.logger.error(f"Failed to gather metrics for user {user}: {user_err}", exc_info=True)
                # Continue with next user instead of breaking entire scrape
                continue

        combined_metrics_response = '\n'.join(global_output_lines) + '\n'
        return Response(combined_metrics_response, mimetype='text/plain')

    except Exception as e:
        app.logger.error(f"Failed to generate server-wide cPanel metrics: {str(e)}", exc_info=True)
        return Response("Internal server error\n", status=500, mimetype='text/plain')


# ------------------------------
# CLI / main
# ------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='cPanel Server Exporter for Prometheus. Scrapes the Statistics panel, '
                    'MySQL, PostGreSQL, FTP and email accounts information for ALL cPanel users '
                    'using WHM API 1 (listaccts) and per-user cPanel UAPI.'
    )
    parser.add_argument('-P', '--port', type=int, default=9123,
                        help='Port to serve the exporter on. Default is 9123.')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Host to bind to. Default is 0.0.0.0.')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_arguments()

    # Require root, because we use whmapi1 and uapi --user
    if os.geteuid() != 0:
        sys.stderr.write(
            "ERROR: This exporter must be run as root, because it uses WHM API 1 and UAPI for all users.\n"
        )
        sys.exit(1)

    app.run(host=args.host, port=args.port)
