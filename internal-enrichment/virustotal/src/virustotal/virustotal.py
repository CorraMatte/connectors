# -*- coding: utf-8 -*-
"""VirusTotal enrichment module."""
import asyncio
import json
from pathlib import Path

import stix2
import yaml
from pycti import OpenCTIConnectorHelper, get_config_variable

from .builder import VirusTotalBuilder
from .client import VirusTotalClient
from .indicator_config import IndicatorConfig


class VirusTotalConnector:
    """VirusTotal connector."""

    _SOURCE_NAME = "VirusTotal"
    _API_URL = "https://www.virustotal.com/api/v3"

    def __init__(self):
        # Instantiate the connector helper from config
        config_file_path = Path(__file__).parent.parent.resolve() / "config.yml"

        config = (
            yaml.load(open(config_file_path, encoding="utf-8"), Loader=yaml.FullLoader)
            if config_file_path.is_file()
            else {}
        )
        self.helper = OpenCTIConnectorHelper(config)
        token = get_config_variable("VIRUSTOTAL_TOKEN", ["virustotal", "token"], config)
        self.max_tlp = get_config_variable(
            "VIRUSTOTAL_MAX_TLP", ["virustotal", "max_tlp"], config
        )
        self.replace_with_lower_score = get_config_variable(
            "VIRUSTOTAL_REPLACE_WITH_LOWER_SCORE",
            ["virustotal", "replace_with_lower_score"],
            config,
        )
        self.author = stix2.Identity(
            name=self._SOURCE_NAME,
            identity_class="Organization",
            description="VirusTotal",
            confidence=self.helper.connect_confidence_level,
        )

        self.client = VirusTotalClient(self.helper, self._API_URL, token)

        # Cache to store YARA rulesets.
        self.yara_cache = {}

        self.bundle = [self.author]

        self.confidence_level = get_config_variable(
            "CONNECTOR_CONFIDENCE_LEVEL",
            ["connector", "confidence_level"],
            config,
            True,
        )

        # File/Artifact specific settings
        self.file_create_note_full_report = get_config_variable(
            "VIRUSTOTAL_FILE_CREATE_NOTE_FULL_REPORT",
            ["virustotal", "file_create_note_full_report"],
            config,
        )
        self.file_upload_unseen_artifacts = get_config_variable(
            "VIRUSTOTAL_FILE_UPLOAD_UNSEEN_ARTIFACTS",
            ["virustotal", "file_upload_unseen_artifacts"],
            config,
        )
        self.file_indicator_config = IndicatorConfig.load_indicator_config(
            config, "FILE"
        )

        # IP specific settings
        self.ip_add_relationships = get_config_variable(
            "VIRUSTOTAL_IP_ADD_RELATIONSHIPS",
            ["virustotal", "ip_add_relationships"],
            config,
        )
        self.ip_indicator_config = IndicatorConfig.load_indicator_config(config, "IP")

        # Domain specific settings
        self.domain_add_relationships = get_config_variable(
            "VIRUSTOTAL_DOMAIN_ADD_RELATIONSHIPS",
            ["virustotal", "domain_add_relationships"],
            config,
        )
        self.domain_indicator_config = IndicatorConfig.load_indicator_config(
            config, "DOMAIN"
        )

        # Url specific settings
        self.url_upload_unseen = get_config_variable(
            "VIRUSTOTAL_URL_UPLOAD_UNSEEN",
            ["virustotal", "url_upload_unseen"],
            config,
        )
        self.url_indicator_config = IndicatorConfig.load_indicator_config(config, "URL")

    def _retrieve_yara_ruleset(self, ruleset_id: str) -> dict:
        """
        Retrieve yara ruleset.

        If the yara is not in the cache, make an API call.

        Returns
        -------
        dict
            YARA ruleset object.
        """
        self.helper.log_debug(f"[VirusTotal] Retrieving ruleset {ruleset_id}")
        if ruleset_id in self.yara_cache:
            self.helper.log_debug(f"Retrieving YARA ruleset {ruleset_id} from cache.")
            ruleset = self.yara_cache[ruleset_id]
        else:
            self.helper.log_debug(f"Retrieving YARA ruleset {ruleset_id} from API.")
            ruleset = self.client.get_yara_ruleset(ruleset_id)
            self.yara_cache[ruleset_id] = ruleset
        return ruleset

    async def _process_file(self, observable):
        json_data = self.client.get_file_info(observable["observable_value"])
        assert json_data
        if (
            "error" in json_data
            and json_data["error"]["code"] == "NotFoundError"
            and self.file_upload_unseen_artifacts
            and observable["entity_type"] == "Artifact"
        ):
            message = f"The file {observable['observable_value']} was not found in VirusTotal repositories. Beginning upload and analysis"
            self.helper.api.work.to_received(self.helper.work_id, message)
            self.helper.log_debug(message)
            # Larger files can sometimes take a few seconds to propogate through the system and be added to the observable
            # It appears to be about 1 second for every 30-50MB
            if not observable["importFiles"]:
                await asyncio.sleep(5)
                observable = self.helper.api.stix_cyber_observable.read(
                    id=observable["id"]
                )
            # File must be smaller than 32MB for VirusTotal upload
            if observable["importFiles"][0]["size"] > 33554432:
                raise ValueError(
                    "The file attempting to be uploaded is greater than VirusTotal's 32MB limit"
                )
            artifact_url = f'{self.helper.opencti_url}/storage/get/{observable["importFiles"][0]["id"]}'
            try:
                artifact = self.helper.api.fetch_opencti_file(artifact_url, binary=True)
            except Exception as err:
                raise ValueError(
                    "[VirusTotal] Error fetching artifact from OpenCTI"
                ) from err
            try:
                analysis_id = self.client.upload_artifact(
                    observable["importFiles"][0]["name"], artifact
                )
                # Attempting to get the file info immediately queues the artifact for more immediate analysis
                self.client.get_file_info(observable["observable_value"])
            except Exception as err:
                raise ValueError(
                    "[VirusTotal] Error uploading artifact to VirusTotal"
                ) from err
            try:
                await self.client.check_upload_status(
                    "artifact", observable["observable_value"], analysis_id
                )
            except Exception as err:
                raise ValueError(
                    "[VirusTotal] Error waiting for VirusTotal to analyze artifact"
                ) from err
            json_data = self.client.get_file_info(observable["observable_value"])
            assert json_data
        if "error" in json_data:
            raise ValueError(json_data["error"]["message"])
        if "data" not in json_data or "attributes" not in json_data["data"]:
            raise ValueError("An error has occurred.")

        builder = VirusTotalBuilder(
            self.helper,
            self.author,
            self.replace_with_lower_score,
            observable,
            json_data["data"],
        )

        builder.update_hashes()

        # Set the size and names (main and additional)
        if observable["entity_type"] == "StixFile":
            builder.update_size()

        builder.update_names(
            observable["entity_type"] == "StixFile"
            and (observable["name"] is None or len(observable["name"]) == 0)
        )

        builder.create_indicator_based_on(
            self.file_indicator_config,
            f"""[file:hashes.'SHA-256' = '{json_data["data"]["attributes"]["sha256"]}']""",
        )

        # Create labels from tags
        builder.update_labels()

        # Add YARA rules (only if a rule is given).
        for yara in json_data["data"]["attributes"].get(
            "crowdsourced_yara_results", []
        ):
            ruleset = self._retrieve_yara_ruleset(
                yara.get("ruleset_id", "No ruleset id provided")
            )
            builder.create_yara(
                yara,
                ruleset,
                json_data["data"]["attributes"].get("creation_date", None),
            )

        # Create a Note with the full report
        if self.file_create_note_full_report:
            builder.create_note(
                "VirusTotal Report", f"```\n{json.dumps(json_data, indent=2)}\n```"
            )
        return builder.send_bundle()

    def _process_ip(self, observable):
        json_data = self.client.get_ip_info(observable["observable_value"])
        assert json_data
        if "error" in json_data:
            raise ValueError(json_data["error"]["message"])
        if "data" not in json_data or "attributes" not in json_data["data"]:
            raise ValueError("An error has occurred.")

        builder = VirusTotalBuilder(
            self.helper,
            self.author,
            self.replace_with_lower_score,
            observable,
            json_data["data"],
        )

        if self.ip_add_relationships:
            builder.create_asn_belongs_to()
            builder.create_location_located_at()

        builder.create_indicator_based_on(
            self.ip_indicator_config,
            f"""[ipv4-addr:value = '{observable["observable_value"]}']""",
        )
        builder.create_notes()
        return builder.send_bundle()

    def _process_domain(self, observable):
        json_data = self.client.get_domain_info(observable["observable_value"])
        assert json_data
        if "error" in json_data:
            raise ValueError(json_data["error"]["message"])
        if "data" not in json_data or "attributes" not in json_data["data"]:
            raise ValueError("An error has occurred.")

        builder = VirusTotalBuilder(
            self.helper,
            self.author,
            self.replace_with_lower_score,
            observable,
            json_data["data"],
        )

        if self.domain_add_relationships:
            # Create IPv4 address observables for each A record
            # and a Relationship between them and the observable.
            for ip in [
                r["value"]
                for r in json_data["data"]["attributes"]["last_dns_records"]
                if r["type"] == "A"
            ]:
                self.helper.log_debug(
                    f'[VirusTotal] adding ip {ip} to domain {observable["observable_value"]}'
                )
                builder.create_ip_resolves_to(ip)

        builder.create_indicator_based_on(
            self.domain_indicator_config,
            f"""[domain-name:value = '{observable["observable_value"]}']""",
        )
        builder.create_notes()
        return builder.send_bundle()

    async def _process_url(self, observable):
        json_data = self.client.get_url_info(observable["observable_value"])
        assert json_data
        if (
            "error" in json_data
            and json_data["error"]["code"] == "NotFoundError"
            and self.url_upload_unseen
        ):
            message = f"The URL {observable['observable_value']} was not found in VirusTotal repositories. Beginning upload and analysis"
            self.helper.api.work.to_received(self.helper.work_id, message)
            self.helper.log_debug(message)
            try:
                analysis_id = self.client.upload_url(observable["observable_value"])
            except Exception as err:
                raise ValueError(
                    "[VirusTotal] Error uploading URL to VirusTotal"
                ) from err
            try:
                await self.client.check_upload_status(
                    "URL", observable["observable_value"], analysis_id
                )
            except Exception as err:
                raise ValueError(
                    "[VirusTotal] Error waiting for VirusTotal to analyze URL"
                ) from err
            json_data = self.client.get_url_info(observable["observable_value"])
            assert json_data
        print(json_data, flush=True)
        if "error" in json_data:
            raise ValueError(json_data["error"]["message"])
        if "data" not in json_data or "attributes" not in json_data["data"]:
            raise ValueError("An error has occurred.")

        builder = VirusTotalBuilder(
            self.helper,
            self.author,
            self.replace_with_lower_score,
            observable,
            json_data["data"],
        )

        builder.create_indicator_based_on(
            self.ip_indicator_config,
            f"""[url:value = '{observable["observable_value"]}']""",
        )
        builder.create_notes()
        return builder.send_bundle()

    async def _process_message(self, data):
        self.helper.metric.inc("run_count")
        self.helper.metric.state("running")
        entity_id = data["entity_id"]
        observable = self.helper.api.stix_cyber_observable.read(id=entity_id)
        if observable is None:
            raise ValueError(
                "Observable not found (or the connector does not has access to this observable, "
                "check the group of the connector user)"
            )

        # Extract TLP
        tlp = "TLP:CLEAR"
        for marking_definition in observable.get("objectMarking", []):
            if marking_definition["definition_type"] == "TLP":
                tlp = marking_definition["definition"]

        if not OpenCTIConnectorHelper.check_max_tlp(tlp, self.max_tlp):
            raise ValueError(
                "Do not send any data, TLP of the observable is greater than MAX TLP"
            )

        self.helper.log_debug(
            f"[VirusTotal] starting enrichment of observable: {observable}"
        )
        match observable["entity_type"]:
            case "StixFile" | "Artifact":
                return await self._process_file(observable)
            case "IPv4-Addr":
                return self._process_ip(observable)
            case "Domain-Name":
                return self._process_domain(observable)
            case "Url":
                return await self._process_url(observable)
            case _:
                raise ValueError(
                    f'{observable["entity_type"]} is not a supported entity type.'
                )

    def start(self):
        """Start the main loop."""
        self.helper.metric.state("idle")
        self.helper.listen(self._process_message)
