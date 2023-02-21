#!/usr/bin/env python3
# Copyright 2022 Guillaume Belanger
# See LICENSE file for licensing details.

"""Charmed operator for the 5G NRF service."""

import logging
from typing import Union

from charms.data_platform_libs.v0.data_interfaces import DatabaseCreatedEvent, DatabaseRequires
from charms.nrf_operator.v0.nrf import NRFProvides
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from jinja2 import Environment, FileSystemLoader
from lightkube.models.core_v1 import ServicePort
from ops.charm import CharmBase, PebbleReadyEvent, RelationCreatedEvent, RelationJoinedEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, ModelError, WaitingStatus
from ops.pebble import Layer

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = "/etc/nrf"
CONFIG_FILE_NAME = "nrfcfg.yaml"
DATABASE_NAME = "free5gc"


class NRFOperatorCharm(CharmBase):
    """Main class to describe juju event handling for the 5G NRF operator."""

    def __init__(self, *args):
        super().__init__(*args)
        self._container_name = self._service_name = "nrf"
        self._container = self.unit.get_container(self._container_name)
        self._database = DatabaseRequires(
            self, relation_name="database", database_name=DATABASE_NAME
        )
        self._nrf_provides = NRFProvides(charm=self, relationship_name="nrf")
        self.framework.observe(self.on.database_relation_joined, self._on_nrf_pebble_ready)
        self.framework.observe(self.on.nrf_pebble_ready, self._on_nrf_pebble_ready)
        self.framework.observe(self.on.nrf_relation_joined, self._on_nrf_relation_joined)
        self.framework.observe(self._database.on.database_created, self._on_database_created)
        self._service_patcher = KubernetesServicePatch(
            charm=self,
            ports=[
                ServicePort(name="sbi", port=29510),
            ],
        )

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Handle database created event."""
        if not self._container.can_connect():
            self.unit.status = WaitingStatus("Waiting for container to be ready")
            event.defer()
            return
        self._write_config_file(
            database_url=event.uris.split(",")[0],
        )
        self._on_nrf_pebble_ready(event)

    def _write_config_file(self, database_url: str) -> None:
        jinja2_environment = Environment(loader=FileSystemLoader("src/templates/"))
        template = jinja2_environment.get_template("nrfcfg.yaml.j2")
        content = template.render(
            database_name=DATABASE_NAME,
            database_url=database_url,
        )
        self._container.push(path=f"{BASE_CONFIG_PATH}/{CONFIG_FILE_NAME}", source=content)
        logger.info(f"Pushed {CONFIG_FILE_NAME} config file")

    @property
    def _config_file_is_written(self) -> bool:
        """Returns whether the config file was written to the workload container."""
        if not self._container.exists(f"{BASE_CONFIG_PATH}/{CONFIG_FILE_NAME}"):
            logger.info(f"Config file is not written: {CONFIG_FILE_NAME}")
            return False
        logger.info("Config file is written")
        return True

    def _on_nrf_pebble_ready(
        self, event: Union[PebbleReadyEvent, RelationCreatedEvent, DatabaseCreatedEvent]
    ) -> None:
        """Handle Pebble ready event."""
        if not self._database_relation_is_created:
            self.unit.status = BlockedStatus("Waiting for database relation to be created")
            return
        if not self._container.can_connect():
            self.unit.status = WaitingStatus("Waiting for container to be ready")
            event.defer()
            return
        if not self._config_file_is_written:
            self.unit.status = WaitingStatus("Waiting for config file to be written")
            return
        self._container.add_layer("nrf", self._pebble_layer, combine=True)
        self._container.replan()
        self.unit.status = ActiveStatus()
        self._update_nrf_relation()

    def _on_nrf_relation_joined(self, event: RelationJoinedEvent) -> None:
        """Handle NRF relation joined event."""
        if not self._nrf_service_is_running:
            return
        self._update_nrf_relation()

    @property
    def _nrf_service_is_running(self) -> bool:
        """Returns whether the NRF service is running."""
        if not self._container.can_connect():
            return False
        try:
            service = self._container.get_service(self._service_name)
        except ModelError:
            return False
        if not service.is_running():
            return False
        return True

    def _update_nrf_relation(self):
        """Update the NRF relation with the URL of the NRF service."""
        self._nrf_provides.set_info(url=self._nrf_url)

    @property
    def _database_relation_is_created(self) -> bool:
        return self._relation_created("database")

    def _relation_created(self, relation_name: str) -> bool:
        """Returns whether a given Juju relation was crated.

        Args:
            relation_name (str): Relation name

        Returns:
            str: Whether the relation was created.
        """
        if not self.model.get_relation(relation_name):
            return False
        return True

    @property
    def _pebble_layer(self) -> Layer:
        """Returns pebble layer for the charm.

        Returns:
            Layer: Pebble Layer
        """
        return Layer(
            {
                "summary": "nrf layer",
                "description": "pebble config layer for nrf",
                "services": {
                    "nrf": {
                        "override": "replace",
                        "startup": "enabled",
                        "command": f"nrf --nrfcfg {BASE_CONFIG_PATH}/{CONFIG_FILE_NAME}",
                        "environment": self._environment_variables,
                    },
                },
            }
        )

    @property
    def _environment_variables(self) -> dict:
        return {
            "GRPC_GO_LOG_VERBOSITY_LEVEL": "99",
            "GRPC_GO_LOG_SEVERITY_LEVEL": "info",
            "GRPC_TRACE": "all",
            "GRPC_VERBOSITY": "debug",
            "MANAGED_BY_CONFIG_POD": "true",
        }

    @property
    def _nrf_url(self) -> str:
        return f"http://{self.model.app.name}.{self.model.name}.svc.cluster.local:29510"


if __name__ == "__main__":
    main(NRFOperatorCharm)
