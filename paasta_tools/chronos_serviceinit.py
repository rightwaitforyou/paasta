#!/usr/bin/env python
# Copyright 2015-2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

import chronos_tools
import humanize
import isodate

from paasta_tools.mesos_tools import get_running_tasks_from_active_frameworks
from paasta_tools.mesos_tools import status_mesos_tasks_verbose
from paasta_tools.utils import _log
from paasta_tools.utils import datetime_from_utc_to_local
from paasta_tools.utils import PaastaColors


log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


# Calls the 'manual start' endpoint in Chronos (https://mesos.github.io/chronos/docs/api.html#manually-starting-a-job),
# running the job now regardless of its 'schedule' and 'disabled' settings. The job's 'schedule' is left unmodified.
def start_chronos_job(service, instance, job_id, client, cluster, job_config, emergency=False):
    name = PaastaColors.cyan(job_id)
    log_reason = PaastaColors.red("EmergencyStart") if emergency else "Brutal bounce"
    log_immediate_run = " and running it immediately" if not job_config["disabled"] else ""
    _log(
        service=service,
        line="%s: Sending job %s to Chronos%s" % (log_reason, name, log_immediate_run),
        component="deploy",
        level="event",
        cluster=cluster,
        instance=instance
    )
    client.update(job_config)
    # TODO fail or give some output/feedback to user that the job won't run immediately if disabled (PAASTA-1244)
    if not job_config["disabled"]:
        client.run(job_id)


def stop_chronos_job(service, instance, client, cluster, existing_jobs, emergency=False):
    log_reason = PaastaColors.red("EmergencyStop") if emergency else "Brutal bounce"
    for job in existing_jobs:
        name = PaastaColors.cyan(job["name"])
        _log(
            service=service,
            line="%s: Killing all tasks for job %s" % (log_reason, name),
            component="deploy",
            level="event",
            cluster=cluster,
            instance=instance
        )
        job["disabled"] = True
        client.update(job)
        client.delete_tasks(job["name"])


def restart_chronos_job(service, instance, job_id, client, cluster, matching_jobs, job_config, emergency=False):
    stop_chronos_job(service, instance, client, cluster, matching_jobs, emergency)
    start_chronos_job(service, instance, job_id, client, cluster, job_config, emergency)


def get_short_task_id(task_id):
    """Return just the Chronos-generated timestamp section of a Mesos task id."""
    return task_id.split(chronos_tools.MESOS_TASK_SPACER)[1]


def _format_job_name(job):
    job_id = job.get("name", PaastaColors.red("UNKNOWN"))
    return job_id


def _format_disabled_status(job):
    status = PaastaColors.red("UNKNOWN")
    if job.get("disabled", False):
        status = PaastaColors.grey("Not scheduled")
    else:
        status = PaastaColors.green("Scheduled")
    return status


def _prettify_time(time):
    """Given a time, return a formatted representation of that time"""
    try:
        dt = isodate.parse_datetime(time)
    except isodate.isoerror.ISO8601Error:
        print "unable to parse datetime %s" % time
        raise
    dt_localtime = datetime_from_utc_to_local(dt)
    pretty_dt = "%s, %s" % (
        dt_localtime.strftime("%Y-%m-%dT%H:%M"),
        humanize.naturaltime(dt_localtime),
    )
    return pretty_dt


def _prettify_status(status):
    if status not in (
        chronos_tools.LastRunState.Fail,
        chronos_tools.LastRunState.Success,
        chronos_tools.LastRunState.NotRun,
    ):
        raise ValueError("Expected valid state, got %s" % status)
    if status == chronos_tools.LastRunState.Fail:
        return PaastaColors.red("Failed")
    elif status == chronos_tools.LastRunState.Success:
        return PaastaColors.green("OK")
    elif status == chronos_tools.LastRunState.NotRun:
        return PaastaColors.yellow("New")


def _format_last_result(job):
    time, status = chronos_tools.get_status_last_run(job)
    if status is chronos_tools.LastRunState.NotRun:
        formatted_time = "never"
    else:
        formatted_time = _prettify_time(time)
    return _prettify_status(status), formatted_time


def _format_schedule(job):
    if job.get('parents') is not None:
        schedule = PaastaColors.yellow("None (Dependent Job).")
    else:
        schedule = job.get("schedule", PaastaColors.red("UNKNOWN"))
    epsilon = job.get("epsilon", PaastaColors.red("UNKNOWN"))
    schedule_time_zone = job.get("scheduleTimeZone", "null")
    if schedule_time_zone == "null":  # This is what Chronos returns.
        schedule_time_zone = "UTC"
    formatted_schedule = "%s (%s) Epsilon: %s" % (schedule, schedule_time_zone, epsilon)
    return formatted_schedule


def _format_parents_summary(parents):
    return " %s" % ",".join(parents)


def _format_parents_verbose(job):
    parents = job.get('parents', [])
    # create (service,instance) pairs for the parent names
    parent_service_instances = [tuple(chronos_tools.decompose_job_id(parent)) for parent in parents]

    # find matching parent jobs
    parent_jobs = [chronos_tools.get_job_for_service_instance(*service_instance)
                   for service_instance in parent_service_instances]

    # get the status of the last run of each parent job
    parent_statuses = [(parent, _format_last_result(job)) for parent in parent_jobs]
    formatted_lines = [("\n"
                        "    - %(job_name)s\n"
                        "      Last Run: %(status)s (%(last_run)s)" % {
                            "job_name": parent['name'],
                            "last_run": status_parent[1],
                            "status": status_parent[0],
                        }) for (parent, status_parent) in parent_statuses]
    return '\n'.join(formatted_lines)


def none_formatter():
    return "None"


def get_schedule_formatter(job_type, verbose):
    """ Given a job type and a verbosity level, return
    a function suitable for formatting details of the job's
    schedule. In the case of a Dependent Job, the fn will
    format the parents of the job, and a schedule in the case
    of a Scheduled Job"""
    if job_type == chronos_tools.JobType.Dependent:
        return _get_parent_formatter(verbose)
    else:
        return _format_schedule


def _get_parent_formatter(verbose):
    """ Returns a formatting function dependent on
    desired verbosity.
    """
    def dispatch_formatter(job):
        parents = job.get('parents')
        if not parents:
            return none_formatter()
        elif verbose:
            return _format_parents_verbose(job)
        else:
            return _format_parents_summary(parents)
    return dispatch_formatter


def _get_schedule_field_for_job_type(job_type):
    if job_type == chronos_tools.JobType.Dependent:
        return 'Parents'
    elif job_type == chronos_tools.JobType.Scheduled:
        return 'Schedule'
    else:
        raise ValueError("Expected a valid JobType")


def _format_command(job):
    command = job.get("command", PaastaColors.red("UNKNOWN"))
    return command


def _format_mesos_status(job, running_tasks):
    mesos_status = PaastaColors.red("UNKNOWN")
    num_tasks = len(running_tasks)
    if num_tasks == 0:
        mesos_status = PaastaColors.grey("Not running")
    elif num_tasks == 1:
        mesos_status = PaastaColors.yellow("Running")
    else:
        mesos_status = PaastaColors.red("Critical - %d tasks running (expected 1)" % num_tasks)
    return mesos_status


def modify_string_for_rerun_status(string, launched_by_rerun):
    """Appends information to include a note about
    being launched by paasta rerun. If the param launched_by_rerun
    is False, then the string returned is an unmodified copy of that provided
    by the string parameter.
    :param string: the string to be modified
    :returns: a string with information about rerun status appended
    """
    if launched_by_rerun:
        return "%s (Launched by paasta rerun)" % string
    else:
        return string


def format_chronos_job_status(job, running_tasks, verbose=0):
    """Given a job, returns a pretty-printed human readable output regarding
    the status of the job.

    :param job: dictionary of the job status
    :param running_tasks: a list of Mesos tasks associated with ``job``, e.g. the
                          result of ``mesos_tools.get_running_tasks_from_active_frameworks()``.
    :param verbose: int verbosity level
    """
    job_name = _format_job_name(job)
    is_temporary = chronos_tools.is_temporary_job(job) if 'name' in job else 'UNKNOWN'
    job_name = modify_string_for_rerun_status(job_name, is_temporary)
    disabled_state = _format_disabled_status(job)

    (last_result, formatted_time) = _format_last_result(job)

    job_type = chronos_tools.get_job_type(job)
    schedule_type = _get_schedule_field_for_job_type(job_type)
    schedule_formatter = get_schedule_formatter(job_type, verbose)
    schedule_value = schedule_formatter(job)

    command = _format_command(job)
    mesos_status = _format_mesos_status(job, running_tasks)
    if verbose > 0:
        tail_stdstreams = verbose > 1
        mesos_status_verbose = status_mesos_tasks_verbose(job["name"], get_short_task_id, tail_stdstreams)
        mesos_status = "%s\n%s" % (mesos_status, mesos_status_verbose)
    return (
        "Job:     %(job_name)s\n"
        "  Status:   %(disabled_state)s"
        "  Last:     %(last_result)s (%(formatted_time)s)\n"
        "  %(schedule_type)s: %(schedule_value)s\n"
        "  Command:  %(command)s\n"
        "  Mesos:    %(mesos_status)s" % {
            "job_name": job_name,
            "is_temporary": is_temporary,
            "schedule_type": schedule_type,
            "disabled_state": disabled_state,
            "last_result": last_result,
            "formatted_time": formatted_time,
            "schedule_value": schedule_value,
            "command": command,
            "mesos_status": mesos_status,
        }
    )


def status_chronos_jobs(jobs, job_config, verbose):
    """Returns a formatted string of the status of a list of chronos jobs

    :param jobs: list of dicts of chronos job info as returned by the chronos
        client
    :param job_config: dict containing configuration about these jobs as
        provided by chronos_tools.load_chronos_job_config().
    :param verbose: int verbosity level
    """
    if jobs == []:
        return "%s: chronos job is not set up yet" % PaastaColors.yellow("Warning")
    else:
        output = []
        desired_state = job_config.get_desired_state_human()
        output.append("Desired:    %s" % desired_state)
        for job in jobs:
            running_tasks = get_running_tasks_from_active_frameworks(job["name"])
            output.append(format_chronos_job_status(job, running_tasks, verbose))
        return "\n".join(output)


def perform_command(command, service, instance, cluster, verbose, soa_dir):
    """Performs a start/stop/restart/status on an instance
    :param command: String of start, stop, restart, status or scale
    :param service: service name
    :param instance: instance name, like "main" or "canary"
    :param cluster: cluster name
    :param verbose: int verbosity level
    :returns: A unix-style return code
    """
    chronos_config = chronos_tools.load_chronos_config()
    client = chronos_tools.get_chronos_client(chronos_config)
    complete_job_config = chronos_tools.create_complete_config(service, instance, soa_dir=soa_dir)
    job_id = complete_job_config["name"]

    if command == "start":
        start_chronos_job(service, instance, job_id, client, cluster, complete_job_config, emergency=True)
    elif command == "stop":
        matching_jobs = chronos_tools.lookup_chronos_jobs(
            service=service,
            instance=instance,
            client=client,
            include_disabled=True,
        )
        stop_chronos_job(service, instance, client, cluster, matching_jobs, emergency=True)
    elif command == "restart":
        matching_jobs = chronos_tools.lookup_chronos_jobs(
            service=service,
            instance=instance,
            client=client,
            include_disabled=True,
        )
        restart_chronos_job(
            service,
            instance,
            job_id,
            client,
            cluster,
            matching_jobs,
            complete_job_config,
            emergency=True,
        )
    elif command == "status":
        # Verbose mode shows previous versions.
        matching_jobs = chronos_tools.lookup_chronos_jobs(
            service=service,
            instance=instance,
            client=client,
            include_disabled=True,
        )
        sorted_matching_jobs = chronos_tools.sort_jobs(matching_jobs)
        job_config = chronos_tools.load_chronos_job_config(
            service=service,
            instance=instance,
            cluster=cluster,
            soa_dir=soa_dir,
        )
        print status_chronos_jobs(sorted_matching_jobs, job_config, verbose)
    else:
        # The command parser shouldn't have let us get this far...
        raise NotImplementedError("Command %s is not implemented!" % command)
    return 0

# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
