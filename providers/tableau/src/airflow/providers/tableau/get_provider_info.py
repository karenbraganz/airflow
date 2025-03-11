# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# NOTE! THIS FILE IS AUTOMATICALLY GENERATED AND WILL BE OVERWRITTEN!
#
# IF YOU WANT TO MODIFY THIS FILE, YOU SHOULD MODIFY THE TEMPLATE
# `get_provider_info_TEMPLATE.py.jinja2` IN the `dev/breeze/src/airflow_breeze/templates` DIRECTORY


def get_provider_info():
    return {
        "package-name": "apache-airflow-providers-tableau",
        "name": "Tableau",
        "description": "`Tableau <https://www.tableau.com/>`__\n",
        "state": "ready",
        "source-date-epoch": 1741509917,
        "versions": [
            "5.0.2",
            "5.0.1",
            "5.0.0",
            "4.6.1",
            "4.6.0",
            "4.5.2",
            "4.5.1",
            "4.5.0",
            "4.4.2",
            "4.4.1",
            "4.4.0",
            "4.3.0",
            "4.2.2",
            "4.2.1",
            "4.2.0",
            "4.1.0",
            "4.0.0",
            "3.0.1",
            "3.0.0",
            "2.1.8",
            "2.1.7",
            "2.1.6",
            "2.1.5",
            "2.1.4",
            "2.1.3",
            "2.1.2",
            "2.1.1",
            "2.1.0",
            "2.0.0",
            "1.0.0",
        ],
        "integrations": [
            {
                "integration-name": "Tableau",
                "external-doc-url": "https://www.tableau.com/",
                "how-to-guide": ["/docs/apache-airflow-providers-tableau/operators.rst"],
                "logo": "/docs/integration-logos/tableau.png",
                "tags": ["service"],
            }
        ],
        "operators": [
            {"integration-name": "Tableau", "python-modules": ["airflow.providers.tableau.operators.tableau"]}
        ],
        "sensors": [
            {"integration-name": "Tableau", "python-modules": ["airflow.providers.tableau.sensors.tableau"]}
        ],
        "hooks": [
            {"integration-name": "Tableau", "python-modules": ["airflow.providers.tableau.hooks.tableau"]}
        ],
        "connection-types": [
            {
                "hook-class-name": "airflow.providers.tableau.hooks.tableau.TableauHook",
                "connection-type": "tableau",
            }
        ],
        "dependencies": ["apache-airflow>=2.9.0", "tableauserverclient>=0.25"],
        "devel-dependencies": [],
    }
