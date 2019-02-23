# -*- encoding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function

from collections import namedtuple
import logging
import os
import time

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse  # type: ignore

import requests
import yaml

from evergreen.build import Build
from evergreen.host import Host
from evergreen.patch import Patch
from evergreen.project import Project
from evergreen.task import Task
from evergreen.stats import TestStats
from evergreen.util import format_evergreen_datetime
from evergreen.version import Version


EvgAuth = namedtuple('EvgAuth', ['username', 'api_key'])

LOGGER = logging.getLogger(__name__)
DEFAULT_API_SERVER = 'http://evergreen.mongodb.com'
CONFIG_FILE_LOCATIONS = [
    os.path.join('.', '.evergreen.yml'),
    os.path.expanduser(os.path.join('~', '.evergreen.yml')),
    os.path.expanduser(os.path.join('~', 'cli_bin', '.evergreen.yml')),
]


def read_evergreen_config():
    """
    Search known location for the evergreen config file.

    :return: First found evergreen configuration.
    """
    for filename in [filename for filename in CONFIG_FILE_LOCATIONS if os.path.isfile(filename)]:
        with open(filename, 'r') as fstream:
            return yaml.safe_load(fstream)

    return None


class _BaseEvergreenApi(object):
    """Base methods for building API objects."""
    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        self._api_server = api_server
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter()
        self.session.mount('{url.scheme}://'.format(url=urlparse(api_server)), adapter)
        if auth:
            self.session.headers.update({
                'Api-User': auth.username,
                'Api-Key': auth.api_key,
            })

    def _create_url(self, endpoint):
        """Format the a call to an endpoint."""
        return '{api_server}/rest/v2{endpoint}'.format(api_server=self._api_server, endpoint=endpoint)

    @staticmethod
    def _log_api_call_time(response, start_time):
        """
        Log how long the api call took.

        :param response: Response from API.
        :param start_time: Time the response was started.
        """
        duration = round(time.time() - start_time, 2)
        if duration > 10:
            LOGGER.info('Request %s took %fs', response.request.url, duration)
        else:
            LOGGER.debug('Request %s took %fs', response.request.url, duration)

    def _call_api(self, url, params=None):
        """
        Make a call to the evergreen api.

        :param url: Url of call to make.
        :param params: parameters to pass to api.
        :return: response from api server.
        """
        start_time = time.time()
        response = self.session.get(url=url, params=params)
        self._log_api_call_time(response, start_time)

        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response):
        """
        Raise an exception with the evergreen message if it exists.

        :param response: response from evergreen api.
        """
        if response.status_code >= 400 and 'error' in response.json():
            raise requests.exceptions.HTTPError(response.json()['error'], response=response)

        response.raise_for_status()

    def _paginate(self, url, params=None):
        """
        Paginate until all results are returned and return a list of all JSON results.

        :param url: url to make request to.
        :param params: parameters to pass to request.
        :return: json list of all results.
        """
        response = self._call_api(url, params)
        json_data = response.json()
        while "next" in response.links:
            if params and 'limit' in params and len(json_data) >= params['limit']:
                break
            response = self._call_api(response.links['next']['url'])
            if response.json():
                json_data.extend(response.json())

        return json_data


class _HostApi(_BaseEvergreenApi):
    """API for hosts endpoints."""

    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        """Create an Evergreen Api object."""
        super(_HostApi, self).__init__(api_server, auth)

    def get_all_hosts(self, params=None):
        """
        Get all hosts in evergreen.

        :param params: parameters to pass to endpoint.
        :return: List of all hosts in evergreen.
        """
        url = self._create_url('/hosts')
        host_list = self._paginate(url, params)
        return [Host(host, self) for host in host_list]


class _ProjectApi(_BaseEvergreenApi):
    """API for project endpoints."""

    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        """Create an Evergreen Api object."""
        super(_ProjectApi, self).__init__(api_server, auth)

    def get_all_projects(self, params=None):
        """
        Get all projects in evergreen.

        :param params: parameters to pass to endpoint.
        :return: List of all projects in evergreen.
        """
        url = self._create_url('/projects')
        project_list = self._paginate(url, params)
        return [Project(project, self) for project in project_list]

    def get_project_by_id(self, project_id, params=None):
        url = self._create_url('/projects/{project_id}'.format(project_id=project_id))
        return Project(self._paginate(url, params), self)

    def get_recent_version_per_project(self, project_id, params=None):
        """
        Get recent versions created in specified project.

        :param project_id: Id of project to query.
        :param params: parameters to pass to endpoint.
        :return: List of recent versions.
        """
        url = self._create_url(
            '/projects/{project_id}/recent_versions'.format(project_id=project_id))
        version_list = self._paginate(url, params)
        return [Version(version) for version in version_list]

    def get_patches_per_project(self, project_id, params=None):
        """
        Get a list of patches for the specified project.

        :param project_id: Id of project to query.
        :param params: parameters to pass to endpoint.
        :return: List of recent patches.
        """
        url = self._create_url('/projects/{project_id}/patches'.format(project_id=project_id))
        patches = self._paginate(url, params)
        return [Patch(patch) for patch in patches]

    def get_recent_patches_by_project(self, project_id, start_at, params=None):
        start_at = format_evergreen_datetime(start_at)
        if not params:
            params = {'start_at': start_at}
        else:
            params['start_at'] = start_at
        return self.get_patches_per_project(project_id, params)

    def test_stats_by_project(self, project_id, params=None):
        """
        Get a patch by patch id.

        :param project_id: Id of patch to query for.
        :param params: Parameters to pass to endpoint.
        :return: Patch queried for.
        """
        url = self._create_url('/projects/{project_id}/test_stats'.format(project_id=project_id))
        test_stats_list = self._paginate(url, params)
        return [TestStats(test_stat) for test_stat in test_stats_list]


class _BuildApi(_BaseEvergreenApi):
    """API for build endpoints."""

    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        """Create an Evergreen Api object."""
        super(_BuildApi, self).__init__(api_server, auth)

    def build_by_id(self, build_id, params=None):
        """
        Get a build by id.

        :param build_id: build id to query.
        :param params: Parameters to pass to endpoint.
        :return: Build queried for.
        """
        url = self._create_url('/build/{build_id}'.format(build_id=build_id))
        return Build(self._paginate(url, params), self)

    def tasks_by_build_id(self, build_id, params=None):
        """
        Get all tasks for a given build_id.

        :param build_id: build_id to query.
        :param params: Dictionary of parameters to pass to query.
        :return: List of tasks for the specified build.
        """
        url = self._create_url('/builds/{build_id}/tasks'.format(build_id=build_id))
        task_list = self._paginate(url, params)
        return [Task(task) for task in task_list]


class _VersionApi(_BaseEvergreenApi):
    """API for version endpoints."""

    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        """Create an Evergreen Api object."""
        super(_VersionApi, self).__init__(api_server, auth)

    def version_by_id(self, version_id, params=None):
        """
        Get version by version id.

        :param version_id: Id of version to query.
        :param params: Dictionary of parameters.
        :return: Version queried for.
        """
        url = self._create_url('/versions/{version_id}'.format(version_id=version_id))
        return Version(self._paginate(url, params), self)

    def builds_by_version(self, version_id, params=None):
        """
        Get all builds for a given Evergreen version_id.

        :param version_id: Version Id to query for.
        :param params: Dictionary of parameters to pass to query.
        :return: List of builds for the specified version.
        """
        url = self._create_url('/version/{version_id}/builds'.format(version_id=version_id))
        build_list = self._paginate(url, params)
        return [Build(build) for build in build_list]


class _PatchApi(_BaseEvergreenApi):
    """API for patch endpoints."""

    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        """Create an Evergreen Api object."""
        super(_PatchApi, self).__init__(api_server, auth)

    def patch_by_id(self, patch_id, params=None):
        """
        Get a patch by patch id.

        :param patch_id: Id of patch to query for.
        :param params: Parameters to pass to endpoint.
        :return: Patch queried for.
        """
        url = self._create_url('/patches/{patch_id}'.format(patch_id=patch_id))
        return Patch(self._call_api(url, params))


class EvergreenApi(_ProjectApi, _BuildApi, _VersionApi, _PatchApi, _HostApi):
    """Access to the Evergreen API Server."""

    def __init__(self, api_server=DEFAULT_API_SERVER, auth=None):
        """Create an Evergreen Api object."""
        super(EvergreenApi, self).__init__(api_server, auth)

    @classmethod
    def get_api(cls, auth=None, use_config_file=False):
        """
        Get an evergreen api instance based on config file settings.

        :param auth: EvgAuth with authentication to use.
        :param use_config_file: attempt to read auth from config file.
        :return: EvergreenApi instance.
        """
        if not auth and use_config_file:
            config = read_evergreen_config()
            auth = EvgAuth(config['user'], config['api_key'])

        return cls(auth=auth)