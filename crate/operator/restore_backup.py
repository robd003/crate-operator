# CrateDB Kubernetes Operator
#
# Licensed to Crate.IO GmbH ("Crate") under one or more contributor
# license agreements.  See the NOTICE file distributed with this work for
# additional information regarding copyright ownership.  Crate licenses
# this file to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.  You may
# obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
# However, if you have executed another commercial license agreement
# with Crate these terms will supersede the license and you may use the
# software solely pursuant to the terms of the relevant commercial agreement.

import asyncio
import logging
import re
from typing import Any, List

import kopf
from aiohttp.client_exceptions import WSServerHandshakeError
from aiopg import Cursor
from kubernetes_asyncio.client import ApiException, CoreV1Api, CustomObjectsApi
from kubernetes_asyncio.client.api_client import ApiClient
from kubernetes_asyncio.stream import WsApiClient
from psycopg2 import DatabaseError, ProgrammingError
from psycopg2.errors import DuplicateTable
from psycopg2.extensions import AsIs, QuotedString, quote_ident

from crate.operator.config import config
from crate.operator.constants import API_GROUP, RESOURCE_CRATEDB, SYSTEM_USERNAME
from crate.operator.cratedb import (
    connection_factory,
    get_cluster_admin_username,
    get_cluster_settings,
    set_cluster_setting,
)
from crate.operator.operations import (
    get_cratedb_resource,
    scale_backup_metrics_deployment,
)
from crate.operator.utils import crate
from crate.operator.utils.kopf import StateBasedSubHandler, subhandler_partial
from crate.operator.utils.kubeapi import (
    get_host,
    get_system_user_password,
    resolve_secret_key_ref,
)
from crate.operator.utils.notifications import send_operation_progress_notification
from crate.operator.webhooks import (
    WebhookAction,
    WebhookAdminUsernameChangedPayload,
    WebhookEvent,
    WebhookFeedbackPayload,
    WebhookOperation,
    WebhookStatus,
)

RESTORE_BACKUP_SECRETS: List[str] = [
    "bucket",
    "secretAccessKey",
    "basePath",
    "accessKeyId",
]
RESTORE_MAX_BYTES_PER_SEC: str = "200mb"
RESTORE_CLUSTER_CONCURRENT_REBALANCE: int = 6
DEFAULT_MAX_BYTES_PER_SEC: str = "40mb"
DEFAULT_CLUSTER_CONCURRENT_REBALANCE: int = 2
CRASH_COMMAND_DELAY: int = 30


def is_valid_snapshot(new: kopf.Body, **kwargs) -> bool:
    """
    This checks if the new snapshot name is valid or not (empty).
    This check is necessary to avoid another restore operation is
    triggered after we reset the snapshot field at the end of a
    completed restore operation.

    :param new: The new CrateDB resource
    """
    try:
        return len(new["spec"]["cluster"]["restoreSnapshot"]["snapshot"]) > 0
    except KeyError:
        return False


def get_crash_pod_name(spec: dict, name: str) -> str:
    """
    Returns the pod name where crash commands should be run.

    :param spec: The CrateDB custom resource definition.
    :param name: The CrateDB custom resource name defining the CrateDB cluster.
    """
    has_master_nodes = "master" in spec["spec"]["nodes"]
    if has_master_nodes:
        return f"crate-master-{name}-0"
    else:
        node_name = spec["spec"]["nodes"]["data"][0]["name"]
        return f"crate-data-{node_name}-{name}-0"


def get_crash_scheme(spec: dict) -> str:
    """
    Return the host scheme for running crash commands.

    :param spec: The CrateDB custom resource definition.
    """
    return "https" if "ssl" in spec["spec"]["cluster"] else "http"


async def drop_repository(cursor: Cursor, repository: str, logger: logging.Logger):
    """
    Drops a backup repository if it exists.

    :param cursor: A database cursor to a current and open database connection.
    :param repository: The name of the repository to drop.
    :param logger: the logger on which we're logging
    """
    try:
        await cursor.execute(
            "SELECT * FROM sys.repositories WHERE name=%s", (repository,)
        )
        row = await cursor.fetchone()
        if row:
            repository_ident = quote_ident(repository, cursor._impl)
            await cursor.execute(f"DROP REPOSITORY {repository_ident}")
    except ProgrammingError as e:
        logger.warning("Failed to drop repository", exc_info=e)


async def run_crash_command(
    namespace: str,
    pod_name: str,
    scheme: str,
    command: str,
    logger,
    delay: int = CRASH_COMMAND_DELAY,
):
    """
    This connects to a CrateDB pod and executes a crash command in the
    ``crate`` container. It returns the result of the execution.

    :param namespace: The Kubernetes namespace of the CrateDB cluster.
    :param pod_name: The pod name where the command should be run.
    :param scheme: The host scheme for running the command.
    :param command: The SQL query that should be run.
    :param logger: the logger on which we're logging
    :param delay: Time in seconds between the retries when executing
        the query.
    """
    async with WsApiClient() as ws_api_client:
        core_ws = CoreV1Api(ws_api_client)
        try:
            exception_logger = logger.exception if config.TESTING else logger.error
            crash_command = [
                "crash",
                "--verify-ssl=false",
                f"--host={scheme}://localhost:4200",
                "-c",
                command,
            ]
            result = await core_ws.connect_get_namespaced_pod_exec(
                namespace=namespace,
                name=pod_name,
                command=crash_command,
                container="crate",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
            )
        except ApiException as e:
            # We don't use `logger.exception()` to not accidentally include sensitive
            # data in the log messages which might be part of the string
            # representation of the exception.
            exception_logger("... failed. Status: %s Reason: %s", e.status, e.reason)
            raise kopf.TemporaryError(delay=delay)
        except WSServerHandshakeError as e:
            # We don't use `logger.exception()` to not accidentally include sensitive
            # data in the log messages which might be part of the string
            # representation of the exception.
            exception_logger("... failed. Status: %s Message: %s", e.status, e.message)
            raise kopf.TemporaryError(delay=delay)
        else:
            return result


async def ensure_no_restore_in_progress(
    namespace: str,
    name: str,
    snapshot: str,
    pod_name: str,
    scheme: str,
    logger: logging.Logger,
):
    """
    This checks if there is a restore operation of the given snapshot
    currently in progress by querying the ``sys.snapshot_restore`` table.
    If there is a restore in progress, it queries the ``sys.shards`` table
    to get the progress of the operation. It sends this information to
    the API and raises a ``kopf.TemporaryError``.
    Use crash here because during a restore the system user password
    might be restored to a different value already.

    :param namespace: The Kubernetes namespace of the CrateDB cluster.
    :param name: The CrateDB custom resource name defining the CrateDB cluster.
    :param snapshot: The name of the snapshot.
    :param pod_name: The pod name where the crash command should be run.
    :param scheme: The host scheme for running the crash command.
    :param logger: the logger on which we're logging
    """

    command = (
        "SELECT * FROM sys.snapshot_restore WHERE "
        f"name='{snapshot}' AND state NOT IN ('SUCCESS', 'FAILURE')"
    )
    result = await run_crash_command(namespace, pod_name, scheme, command, logger)
    if snapshot in result:
        progress_command = (
            "SELECT min(recovery['size']['percent']) FROM sys.shards "
            "where state='RECOVERING' and recovery['type']='SNAPSHOT';"
        )
        result = await run_crash_command(
            namespace, pod_name, scheme, progress_command, logger
        )
        pct = int(re.findall(r"(\d+)", result)[0]) or 0
        await send_operation_progress_notification(
            namespace=namespace,
            name=name,
            message=f"Please wait while cluster data is being restored... ({pct}%)",
            logger=logger,
            status=WebhookStatus.IN_PROGRESS,
            operation=WebhookOperation.UPDATE,
            action=WebhookAction.RESTORE_SNAPSHOT,
        )
        raise kopf.TemporaryError(
            "A snapshot restore is currently in progress "
            f"({pct}% done), waiting for it to finish...",
            delay=15,
        )


async def get_source_backup_repository_data(
    core: CoreV1Api,
    namespace: str,
    name: str,
    logger: logging.Logger,
) -> dict:
    """
    Read the secret values to access the backup repository of the source
    cluster defined by ``secretKeyRef`` in ``restoreSnapshot``.

    :param core: An instance of the Kubernetes Core V1 API.
    :param namespace: The namespace where to lookup the secret and its value.
    :param name: The CrateDB custom resource name defining the CrateDB cluster.
    :param logger: the logger on which we're logging
    """
    data = {}
    cratedb = await get_cratedb_resource(namespace, name)
    for key in RESTORE_BACKUP_SECRETS:
        try:
            secret_key_ref = cratedb["spec"]["cluster"]["restoreSnapshot"][key][
                "secretKeyRef"
            ]
            data[key] = await resolve_secret_key_ref(
                core,
                namespace,
                secret_key_ref,
            )
        except ApiException as e:
            logger.warning("Reading secret failed: %s", str(e))
            raise kopf.PermanentError(
                f'Secret {secret_key_ref["name"]} could not be found.'
            )
        except KeyError:
            raise kopf.PermanentError(f"Key {key} not found in secret.")

    return data


async def get_snapshot_tables(
    cursor: Cursor, snapshot: str, logger: logging.Logger
) -> List[Any]:
    """
    Returns a list of tables included in a snapshot.

    :param cursor: A database cursor to a current and open database connection.
    :param snapshot: The name of the snapshot where to lookup the tables.
    :param logger: the logger on which we're logging
    """
    try:
        await cursor.execute(
            "SELECT tables FROM sys.snapshots WHERE name=%s", (str(snapshot),)
        )
        row = await cursor.fetchone()
        return row[0] if row else []
    except ProgrammingError as e:
        logger.warning("Failed to get snapshot tables.", exc_info=e)
        return []


async def shards_recovery_in_progress(
    cursor: Cursor,
    snapshot: str,
    tables: List[str],
    logger: logging.Logger,
):
    """
    Checks if there is at least one shard which has not fully recovered after an
    operation of type ``SNAPSHOT``.

    :param cursor: A database cursor to a current and open database connection.
    :param snapshot: The name of the snapshot to restore.
    :param tables: A list of tables which should be checked for shards that have
        not been restored completely.
    :param logger: the logger on which we're logging
    """
    if not tables or (len(tables) == 1 and tables[0].lower() == "all"):
        tables = await get_snapshot_tables(cursor, snapshot, logger)
    for t in tables:
        (schema, table_name) = t.rsplit(".", 1)
        try:
            await cursor.execute(
                "SELECT id FROM sys.shards WHERE schema_name = %s "
                "AND table_name = %s "
                "AND primary = TRUE "
                "LIMIT 1;",
                (schema, table_name),
            )
            primary_shard_exists = await cursor.fetchone()
            await cursor.execute(
                "SELECT id FROM sys.shards WHERE schema_name = %s "
                "AND table_name = %s "
                "AND (state = 'RECOVERING' AND recovery['type'] = 'SNAPSHOT' "
                "AND recovery['size']['percent'] < 100) "
                "AND primary = TRUE "
                "LIMIT 1;",
                (schema, table_name),
            )
            shard_in_progress = await cursor.fetchone()

            if not primary_shard_exists or shard_in_progress:
                logger.info(
                    f"Table {schema}.{table_name} was not restored successfully."
                )
                raise kopf.PermanentError(
                    "Insufficient disc space. Please either expand storage "
                    "or scale up the number of nodes."
                )
        except DatabaseError as e:
            logger.warning("DatabaseError in shards_recovery_in_progress", exc_info=e)
            raise kopf.PermanentError("Shards could not be fetched.")


class BeforeRestoreBackupSubHandler(StateBasedSubHandler):
    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        patch: kopf.Patch,
        logger: logging.Logger,
        **kwargs: Any,
    ):
        await send_operation_progress_notification(
            namespace=namespace,
            name=name,
            message="Preparing to restore data from snapshot.",
            logger=logger,
            status=WebhookStatus.IN_PROGRESS,
            operation=WebhookOperation.UPDATE,
            action=WebhookAction.RESTORE_SNAPSHOT,
        )
        kopf.register(
            fn=subhandler_partial(
                self._prepare_cluster_settings, namespace, name, patch, logger
            ),
            id="prepare_cluster_settings",
        )
        kopf.register(
            fn=subhandler_partial(
                self._suspend_backup_metrics, namespace, name, logger
            ),
            id="suspend_backup_metrics",
        )

    async def _prepare_cluster_settings(
        self, namespace: str, name: str, patch: kopf.Patch, logger: logging.Logger
    ):
        """
        This reads (and updates during the restore operation) the cluster settings
        ``cluster.routing.allocation.cluster_concurrent_rebalance`` and
        ``indices.recovery.max_bytes_per_sec`` and preserves the old values in the
        status object of the CrateDB crd to be able to reset them after the restore.

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param patch: The ``kopf.Patch`` object to store the old settings values.
        :param logger: the logger on which we're logging
        """
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            password, host = await asyncio.gather(
                get_system_user_password(core, namespace, name),
                get_host(core, namespace, name),
            )
            conn_factory = connection_factory(host, password)
            try:
                async with conn_factory() as conn:
                    async with conn.cursor() as cursor:
                        cluster_settings = await get_cluster_settings(cursor)
                        max_bytes_per_sec = (
                            cluster_settings.get("indices", {})
                            .get("recovery", {})
                            .get("max_bytes_per_sec", DEFAULT_MAX_BYTES_PER_SEC)
                        )
                        cluster_concurrent_rebalance = (
                            cluster_settings.get("cluster", {})
                            .get("routing", {})
                            .get("allocation", {})
                            .get(
                                "cluster_concurrent_rebalance",
                                DEFAULT_CLUSTER_CONCURRENT_REBALANCE,
                            )
                        )
                        patch.status["maxBytesPerSec"] = max_bytes_per_sec
                        patch.status[
                            "clusterConcurrentRebalance"
                        ] = cluster_concurrent_rebalance
                        # update the settings during restore operation
                        await set_cluster_setting(
                            conn_factory,
                            logger,
                            setting="cluster.routing.allocation.cluster_concurrent_rebalance",  # noqa
                            value=RESTORE_CLUSTER_CONCURRENT_REBALANCE,
                            mode="PERSISTENT",
                        )
                        await set_cluster_setting(
                            conn_factory,
                            logger,
                            setting="indices.recovery.max_bytes_per_sec",
                            value=RESTORE_MAX_BYTES_PER_SEC,
                            mode="PERSISTENT",
                        )
            except (DatabaseError, asyncio.exceptions.TimeoutError):
                raise kopf.TemporaryError(
                    "Could not read settings.",
                )

    async def _suspend_backup_metrics(
        self, namespace: str, name: str, logger: logging.Logger
    ):
        await scale_backup_metrics_deployment(namespace, name, 0)


class RestoreBackupSubHandler(StateBasedSubHandler):
    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        repository: str,
        snapshot: str,
        tables: List,
        logger: logging.Logger,
        **kwargs: Any,
    ):
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            data = await get_source_backup_repository_data(
                core,
                namespace,
                name,
                logger,
            )
            password, host = await asyncio.gather(
                get_system_user_password(core, namespace, name),
                get_host(core, namespace, name),
            )
            conn_factory = connection_factory(host, password)

            await self._create_backup_repository(conn_factory, repository, data, logger)

            await self._ensure_snapshot_exists(
                conn_factory, repository, snapshot, logger
            )

            await self._start_restore_snapshot(
                conn_factory, repository, snapshot, tables, logger
            )

    @staticmethod
    async def _create_backup_repository(
        conn_factory,
        repository: str,
        data: dict,
        logger: logging.Logger,
    ):
        """
        Create a backup repository with the given credentials if it does not exist yet.

        :param conn_factory: A function that establishes a database connection to
            the CrateDB cluster used for SQL queries.
        :param repository: The name of the repository to be created.
        :param data: a dict containing the bucket name, base path and secrets to access
            the source backup repository.
        :param logger: the logger on which we're logging
        """
        try:
            async with conn_factory() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM sys.repositories WHERE name=%s", (repository,)
                    )
                    row = await cursor.fetchone()
                    if not row:
                        repository_ident = quote_ident(repository, cursor._impl)
                        await cursor.execute(
                            f"CREATE REPOSITORY {repository_ident} type s3 with "
                            "(access_key = %s, secret_key = %s, bucket = %s, "
                            "base_path = %s, readonly=true)",
                            (
                                data["accessKeyId"],
                                data["secretAccessKey"],
                                data["bucket"],
                                data["basePath"],
                            ),
                        )
        except DatabaseError as e:
            logger.warning("DatabaseError in _create_backup_repository", exc_info=e)
            raise kopf.PermanentError("Backup repository could not be created.")

    @staticmethod
    async def _ensure_snapshot_exists(
        conn_factory,
        repository: str,
        snapshot: str,
        logger: logging.Logger,
    ):
        """
        Verify that the snapshot to restore really exists in the given repository.

        :param conn_factory: A function that establishes a database connection to
            the CrateDB cluster used for SQL queries.
        :param repository: The name of the repository.
        :param snapshot: The name of the snapshot to restore.
        :param logger: the logger on which we're logging
        """
        try:
            async with conn_factory() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM sys.snapshots WHERE repository=%s "
                        "AND name=%s LIMIT 1;",
                        (
                            repository,
                            snapshot,
                        ),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        raise kopf.PermanentError(
                            f"Snapshot {snapshot} does not exist "
                            f"in repository {repository}."
                        )
        except DatabaseError as e:
            logger.warning("DatabaseError in _ensure_snapshot_exists", exc_info=e)
            raise kopf.PermanentError("Snapshots could not be fetched.")

    @staticmethod
    async def _start_restore_snapshot(
        conn_factory,
        repository: str,
        snapshot: str,
        tables: List,
        logger: logging.Logger,
    ):
        """
        Run the ``RESTORE SNAPSHOT`` command to start the restore operation in the
        target CrateDB cluster.

        :param conn_factory: A function that establishes a database connection to
            the CrateDB cluster used for SQL queries.
        :param repository: The name of the repository.
        :param snapshot: The name of the snapshot to restore.
        :param tables: The list of tables that should be restored.
        :param logger: the logger on which we're logging
        """
        if not tables or (len(tables) == 1 and tables[0].lower() == "all"):
            tables_str = "all"
        else:
            tables_str = f'TABLE {",".join(tables)}'
        try:
            async with conn_factory() as conn:
                async with conn.cursor() as cursor:
                    repository_ident = quote_ident(repository, cursor._impl)
                    snapshot_ident = quote_ident(snapshot, cursor._impl)

                    await cursor.execute(
                        f"RESTORE SNAPSHOT {repository_ident}.{snapshot_ident} "
                        f"{AsIs(tables_str)} with (wait_for_completion=false)"
                    )
        except DuplicateTable as e:
            logger.warning("Relation already exists.", exc_info=e)
            raise kopf.PermanentError("Relation with the same name already exists.")
        except DatabaseError as e:
            logger.warning("DatabaseError in _start_restore_snapshot", exc_info=e)
            raise kopf.PermanentError("Snapshot could not be restored")


class RestoreSystemUserPasswordSubHandler(StateBasedSubHandler):
    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        logger: logging.Logger,
        **kwargs: Any,
    ):
        """
        Restore the system user password from the secret in the namespace.
        Use crash here because during a restore the system user password was
        probably set to a different value.

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param logger: the logger on which we're logging
        """
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            password = await get_system_user_password(core, namespace, name)
            password_quoted = QuotedString(password).getquoted().decode()

            cratedb = await get_cratedb_resource(namespace, name)
            pod_name = get_crash_pod_name(cratedb, name)
            scheme = get_crash_scheme(cratedb)

            # Cloning a cluster will result in the destruction of all target cluster
            # users.
            # In order for the cluster to operate normally we need to restore the
            # system user password.

            # Reset the system user with the password from the CRD
            command = (
                f'ALTER USER "{SYSTEM_USERNAME}" SET (password={password_quoted});'
            )
            result = await run_crash_command(
                namespace, pod_name, scheme, command, logger
            )
            if "ALTER OK" in result:
                logger.info("... success")
            else:
                logger.info("... error. %s", result)
                raise kopf.TemporaryError(delay=config.BOOTSTRAP_RETRY_DELAY)


async def update_cratedb_admin_username_in_cratedb(
    namespace, cluster_name, new_admin_username
):
    async with ApiClient() as api_client:
        coapi = CustomObjectsApi(api_client)

        await coapi.patch_namespaced_custom_object(
            namespace=namespace,
            group=API_GROUP,
            version="v1",
            plural=RESOURCE_CRATEDB,
            name=cluster_name,
            body=[
                {
                    "op": "replace",
                    "path": "/spec/users/0/name",
                    "value": new_admin_username,
                },
            ],
        )


class ValidateRestoreCompleteSubHandler(StateBasedSubHandler):
    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        snapshot: str,
        tables: List[str],
        logger: logging.Logger,
        **kwargs: Any,
    ):
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            password, host = await asyncio.gather(
                get_system_user_password(core, namespace, name),
                get_host(core, namespace, name),
            )
            conn_factory = connection_factory(host, password)
            async with conn_factory() as conn:
                async with conn.cursor() as cursor:
                    await shards_recovery_in_progress(cursor, snapshot, tables, logger)


class AfterRestoreBackupSubHandler(StateBasedSubHandler):
    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        status: kopf.Status,
        repository: str,
        logger: logging.Logger,
        **kwargs: Any,
    ):
        kopf.register(
            fn=subhandler_partial(
                self._reset_cluster_settings,
                namespace,
                name,
                logger,
                status,
            ),
            id="reset_cluster_settings",
        )
        kopf.register(
            fn=subhandler_partial(
                self._drop_backup_repository, namespace, name, logger, repository
            ),
            id="drop_backup_repository",
        )
        kopf.register(
            fn=subhandler_partial(
                self._delete_backup_credentials_secret, namespace, name, logger
            ),
            id="delete_secret",
        )
        kopf.register(
            fn=subhandler_partial(
                self._restart_backup_metrics, namespace, name, logger
            ),
            id="restart_backup_metrics",
        )

    async def _reset_cluster_settings(
        self,
        namespace: str,
        name: str,
        logger: logging.Logger,
        status: kopf.Status,
    ):
        """
        This checks if all shards have been restored and resets the cluster
        settings ``cluster.routing.allocation.cluster_concurrent_rebalance``
        and ``indices.recovery.max_bytes_per_sec`` to the values before the
        restore operation.

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param logger: the logger on which we're logging
        :param status: kopf.Status to retrieve the preserved settings values.
        :param snapshot: The name of the snapshot to check if it has been
            restored completely.
        """
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            password, host = await asyncio.gather(
                get_system_user_password(core, namespace, name),
                get_host(core, namespace, name),
            )
            conn_factory = connection_factory(host, password)

            # set back settings to the preserved values
            cluster_concurrent_rebalance = (
                status.get("clusterConcurrentRebalance")
                or DEFAULT_CLUSTER_CONCURRENT_REBALANCE
            )
            max_bytes_per_sec = (
                status.get("maxBytesPerSec") or DEFAULT_MAX_BYTES_PER_SEC
            )
            logger.info(
                "restored settings... max_bytes_per_sec: %s, "
                "cluster_concurrent_rebalance: %s",
                max_bytes_per_sec,
                cluster_concurrent_rebalance,
            )
            await set_cluster_setting(
                conn_factory,
                logger,
                setting="cluster.routing.allocation.cluster_concurrent_rebalance",  # noqa
                value=cluster_concurrent_rebalance,
                mode="PERSISTENT",
            )
            await set_cluster_setting(
                conn_factory,
                logger,
                setting="indices.recovery.max_bytes_per_sec",
                value=max_bytes_per_sec,
                mode="PERSISTENT",
            )

    async def _drop_backup_repository(
        self, namespace: str, name: str, logger: logging.Logger, repository: str
    ):
        """
        Drop the temporary backup repository from the target CrateDB cluster.

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param logger: the logger on which we're logging
        :param repository: The name of the repository to drop.
        """
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            password, host = await asyncio.gather(
                get_system_user_password(core, namespace, name),
                get_host(core, namespace, name),
            )
            conn_factory = connection_factory(host, password)
            try:
                async with conn_factory() as conn:
                    async with conn.cursor() as cursor:
                        await drop_repository(cursor, repository, logger)
            except (DatabaseError, asyncio.exceptions.TimeoutError) as e:
                logger.warning("Drop repository operation failed: %s", str(e))
                raise kopf.TemporaryError("Drop repository operation failed.")

    async def _delete_backup_credentials_secret(
        self, namespace: str, name: str, logger: logging.Logger
    ):
        """
        Delete the temporary secret containing the source backup credentials. For
        safety reasons we do not get the secret's name from the ``restoreSnapshot``
        section in the CrateDB resource but rather only delete the secret if it
        was named as defined in ``config.RESTORE_BACKUP_SECRET_NAME``

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param logger: the logger on which we're logging
        """
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            try:
                await core.delete_namespaced_secret(
                    namespace=namespace,
                    name=config.RESTORE_BACKUP_SECRET_NAME.format(name=name),
                )
            except ApiException as e:
                logger.warning("Deleting secret failed: %s", str(e))

    async def _restart_backup_metrics(
        self, namespace: str, name: str, logger: logging.Logger
    ):
        await scale_backup_metrics_deployment(namespace, name, 1)


class SendSuccessNotificationSubHandler(StateBasedSubHandler):
    """
    A handler which depends on all other subhandlers having finished successfully
    and schedules a success notification of the restore process.
    """

    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        logger: logging.Logger,
        **kwargs: Any,
    ):
        """
        Schedule success notification and send it after the cluster
        has been restored successfully.

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param logger: the logger on which we're logging
        """

        # Cloning a cluster will result in the destruction of all target cluster users.
        # We want to update the CrateDB CRD with the admin username, if it has changed.

        # Determine if the admin username has changed.
        async with ApiClient() as api_client:
            core = CoreV1Api(api_client)
            password = await get_system_user_password(core, namespace, name)
            host = await get_host(core, namespace, name)
            conn_factory = connection_factory(host, password)

            # Retrieve admin username from the CrateDB CRD
            cratedb = await get_cratedb_resource(namespace, name)
            crd_users = cratedb["spec"].get("users", {})
            crd_username = crd_users[0]["name"] if len(crd_users) else None

            admin_username = await get_cluster_admin_username(conn_factory, logger)

            # If affirmative, we need to use the source cluster username instead.
            if admin_username and admin_username != crd_username:
                # Write to the Crate CRD the new system username
                await update_cratedb_admin_username_in_cratedb(
                    namespace, name, admin_username
                )
                # Notify the API to update the cluster information
                self.schedule_notification(
                    WebhookEvent.ADMIN_USERNAME_CHANGED,
                    WebhookAdminUsernameChangedPayload(admin_username=admin_username),
                    WebhookStatus.SUCCESS,
                )

        self.schedule_notification(
            WebhookEvent.FEEDBACK,
            WebhookFeedbackPayload(
                message="The snapshot has been restored successfully.",
                operation=WebhookOperation.UPDATE,
                action=WebhookAction.RESTORE_SNAPSHOT,
            ),
            WebhookStatus.SUCCESS,
        )


class ResetSnapshotSubHandler(StateBasedSubHandler):
    @crate.on.error(error_handler=crate.send_update_failed_notification)
    async def handle(  # type: ignore
        self,
        namespace: str,
        name: str,
        logger: logging.Logger,
        **kwargs: Any,
    ):
        """
        Reset the snapshot name in the CrateDB spec to ensure the same snapshot can
        be restored again if it failed for any reason. This has to be done last
        because kopf recognizes it as a new change in the restoreSnapshot field.

        :param namespace: The Kubernetes namespace of the CrateDB cluster.
        :param name: The CrateDB custom resource name defining the CrateDB cluster.
        :param logger: the logger on which we're logging
        """
        async with ApiClient() as api_client:
            coapi = CustomObjectsApi(api_client)
            await coapi.patch_namespaced_custom_object(
                group=API_GROUP,
                version="v1",
                plural=RESOURCE_CRATEDB,
                namespace=namespace,
                name=name,
                body=[
                    {
                        "op": "replace",
                        "path": "/spec/cluster/restoreSnapshot/snapshot",
                        "value": "",
                    }
                ],
            )
