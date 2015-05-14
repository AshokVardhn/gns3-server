# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import aiohttp
import asyncio
import json
import os

from ...web.route import Route
from ...schemas.project import PROJECT_OBJECT_SCHEMA, PROJECT_CREATE_SCHEMA, PROJECT_UPDATE_SCHEMA, PROJECT_FILE_LIST_SCHEMA
from ...modules.project_manager import ProjectManager
from ...modules import MODULES
from ...utils.asyncio import wait_run_in_executor

import logging
log = logging.getLogger()


class ProjectHandler:

    # How many clients has subcribe to notifications
    _notifications_listening = 0

    @classmethod
    @Route.post(
        r"/projects",
        description="Create a new project on the server",
        status_codes={
            201: "Project created",
            409: "Project already created"
        },
        output=PROJECT_OBJECT_SCHEMA,
        input=PROJECT_CREATE_SCHEMA)
    def create_project(request, response):

        pm = ProjectManager.instance()
        p = pm.create_project(
            name=request.json.get("name"),
            path=request.json.get("path"),
            project_id=request.json.get("project_id"),
            temporary=request.json.get("temporary", False)
        )
        response.set_status(201)
        response.json(p)

    @classmethod
    @Route.get(
        r"/projects/{project_id}",
        description="Get project information",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            200: "Success",
            404: "The project doesn't exist"
        },
        output=PROJECT_OBJECT_SCHEMA)
    def show(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        response.json(project)

    @classmethod
    @Route.put(
        r"/projects/{project_id}",
        description="Update a project",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            200: "The project has been updated",
            403: "You are not allowed to modify this property",
            404: "The project doesn't exist"
        },
        output=PROJECT_OBJECT_SCHEMA,
        input=PROJECT_UPDATE_SCHEMA)
    def update(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        project.name = request.json.get("name", project.name)
        project_path = request.json.get("path", project.path)
        if project_path != project.path:
            project.path = project_path
            for module in MODULES:
                yield from module.instance().project_moved(project)
        # Very important we need to remove temporary flag after moving the project
        project.temporary = request.json.get("temporary", project.temporary)
        response.json(project)

    @classmethod
    @Route.post(
        r"/projects/{project_id}/commit",
        description="Write changes on disk",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            204: "Changes have been written on disk",
            404: "The project doesn't exist"
        })
    def commit(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        yield from project.commit()
        response.set_status(204)

    @classmethod
    @Route.post(
        r"/projects/{project_id}/close",
        description="Close a project",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            204: "The project has been closed",
            404: "The project doesn't exist"
        })
    def close(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        if ProjectHandler._notifications_listening == 0:
            yield from project.close()
            pm.remove_project(project.id)
        response.set_status(204)

    @classmethod
    @Route.delete(
        r"/projects/{project_id}",
        description="Delete a project from disk",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            204: "Changes have been written on disk",
            404: "The project doesn't exist"
        })
    def delete(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        yield from project.delete()
        pm.remove_project(project.id)
        response.set_status(204)

    @classmethod
    @Route.get(
        r"/projects/{project_id}/notifications",
        description="Receive notifications about the projects",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            200: "End of stream",
            404: "The project doesn't exist"
        })
    def notification(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])

        response.content_type = "application/json"
        response.set_status(200)
        response.enable_chunked_encoding()
        # Very important: do not send a content lenght otherwise QT close the connection but curl can consume the Feed
        response.content_length = None

        response.start(request)
        queue = project.get_listen_queue()
        ProjectHandler._notifications_listening += 1
        response.write("{\"action\": \"ping\"}\n".encode("utf-8"))
        while True:
            try:
                (action, msg) = yield from asyncio.wait_for(queue.get(), 5)
                if hasattr(msg, "__json__"):
                    msg = json.dumps({"action": action, "event": msg.__json__()}, sort_keys=True)
                else:
                    msg = json.dumps({"action": action, "event": msg}, sort_keys=True)
                log.debug("Send notification: %s", msg)
                response.write(("{}\n".format(msg)).encode("utf-8"))
            except asyncio.futures.CancelledError as e:
                break
            except asyncio.futures.TimeoutError as e:
                response.write("{\"action\": \"ping\"}\n".encode("utf-8"))
        project.stop_listen_queue(queue)
        ProjectHandler._notifications_listening -= 1

    @classmethod
    @Route.get(
        r"/projects/{project_id}/files",
        description="List files of a project",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            200: "Return list of files",
            404: "The project doesn't exist"
        },
        output=PROJECT_FILE_LIST_SCHEMA)
    def list_files(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        files = yield from project.list_files()
        response.json(files)
        response.set_status(200)

    @classmethod
    @Route.get(
        r"/projects/{project_id}/files/{path:.+}",
        description="Get a file of a project",
        parameters={
            "project_id": "The UUID of the project",
        },
        status_codes={
            200: "Return the file",
            403: "Permission denied",
            404: "The file doesn't exist"
        })
    def get_file(request, response):

        pm = ProjectManager.instance()
        project = pm.get_project(request.match_info["project_id"])
        path = request.match_info["path"]
        path = os.path.normpath(path)

        # Raise error if user try to escape
        if path[0] == ".":
            raise aiohttp.web.HTTPForbidden
        path = os.path.join(project.path, path)

        response.content_type = "application/octet-stream"
        response.set_status(200)
        response.enable_chunked_encoding()
        # Very important: do not send a content length otherwise QT close the connection but curl can consume the Feed
        response.content_length = None

        try:
            yield from wait_run_in_executor(ProjectHandler._read_file, path, request, response)
        except FileNotFoundError:
            raise aiohttp.web.HTTPNotFound()
        except PermissionError:
            raise aiohttp.web.HTTPForbidden

    @staticmethod
    def _read_file(path, request, response):

        with open(path, "rb") as f:
            response.start(request)
            while True:
                data = f.read(4096)
                if not data:
                    break
                response.write(data)
