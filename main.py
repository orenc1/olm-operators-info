import base64
import datetime
import json
import logging
import os

import semver as semver
import yaml

INDEX_IMAGES_TAG = "v4.11"
RAW_DATA_DIR = "raw_data"
RENDERED_INFO_DIR = "rendered_info"
ROOT_PATH = os.path.dirname(os.path.realpath(__file__))
LOGLEVEL = os.getenv('LOGLEVEL', 'info')


class OperatorsPoller:
    def __init__(self, index_images_names):
        self.start_time = datetime.datetime.now()
        loglevel = get_loglevel()
        logging.basicConfig(format='%(levelname)s: %(asctime)s - %(message)s', datefmt='%d-%b-%y %H:%M:%S',
                            level=loglevel)
        self.index_images_names = index_images_names
        self.indices_list = []
        for index in self.index_images_names:
            self.indices_list.append(OperatorIndex(index))

        for index in self.indices_list:
            index.extract_index()
            for operator in index.operators_list:
                operator.get_info()

            logging.info(f"Finished processing index image: {index.index_name}.")

    def dump_jsons(self):
        os.makedirs(RENDERED_INFO_DIR, exist_ok=True)
        os.chdir(RENDERED_INFO_DIR)
        for index in self.indices_list:
            index_json = {'operators_list': []}
            for operator in index.operators_list:
                index_json['operators_list'].append(operator.toJson())
            with open(f"{index.index_name}.json", 'w') as fh:
                fh.write(json.dumps(index_json, indent=4))


class OperatorIndex:
    def __init__(self, index_name):
        self.index_name = index_name
        self.index_pullspec = f"registry.redhat.io/redhat/{self.index_name}:{INDEX_IMAGES_TAG}"
        self.operators_list = []

    def toJson(self):
        return [operator.toJson() for operator in self.operators_list]

    def extract_index(self):
        self.index_dir = os.path.join(RAW_DATA_DIR, self.index_name)
        os.makedirs(self.index_dir, exist_ok=True)
        os.chdir(self.index_dir)
        logging.info(f"extracting contents of {self.index_pullspec}...")
        os.system(f"oc image extract {self.index_pullspec} --path=/configs/:. --confirm")
        operators = os.listdir(os.path.join(ROOT_PATH, self.index_dir))
        for operator in operators:
            self.operators_list.append(OperatorInfo(operator, self))
        os.chdir(ROOT_PATH)


class OperatorInfo:
    package_name: str
    latest_version: str
    latest_channel: str
    display_name: str
    disconnected_supported: bool
    fips_supported: bool
    repository: str
    capabilities: str
    documentation_url: str

    def __init__(self, package_name, index_obj):
        self.package_name = package_name
        self.index = index_obj

    def toJson(self):
        return {
            'package_name': self.package_name,
            'display_name': self.display_name,
            'latest_version': self.latest_version,
            'latest_channel': self.latest_channel,
            'disconnected_supported': self.disconnected_supported,
            'fips_supported': self.fips_supported,
            'capabilities': self.capabilities,
            'repository': self.repository,
            'documentation_url': self.documentation_url,
        }

    def get_info(self):
        package_path = os.path.join(ROOT_PATH, self.index.index_dir, self.package_name)
        package_files = os.listdir(package_path)
        if any('json' in file for file in package_files):
            schema_list = self.get_schema_list_json(package_path)
        else:
            schema_list = self.get_schema_list_yaml(package_path)

        versions_list = []
        # entries of the list are tuples of (version, channel name)
        for schema in schema_list:
            if schema['schema'] == 'olm.channel':
                for entry in schema['entries']:
                    ver = entry['name'].split('.v')[1] if '.v' in entry['name'] else '.'.join(
                        entry['name'].split('.')[1:])
                    if semver.VersionInfo.isvalid(ver):
                        versions_list.append((semver.VersionInfo.parse(ver), schema['name']))

        sorted_versions_list = sorted(versions_list, key=lambda tup: tup[0], reverse=True)
        self.latest_version = str(sorted_versions_list[0][0])
        self.latest_channel = str(sorted_versions_list[0][1])
        self.disconnected_supported = False
        self.fips_supported = False

        csv = None
        for schema in schema_list:
            if schema['schema'] == 'olm.bundle' and not csv:
                if schema['name'].endswith(self.latest_version):
                    for property in schema['properties']:
                        if property['type'] == 'olm.bundle.object':
                            jsondata = base64.b64decode(property['value']['data'])
                            bundle_object = json.loads(jsondata)
                            if bundle_object['kind'] == 'ClusterServiceVersion':
                                csv = bundle_object
                                break
        if not csv:
            logging.error(f"CSV of {self.package_name}, version {self.latest_version} could not be found.")
            return

        self.display_name = csv['spec']['displayName']
        if 'operators.openshift.io/infrastructure-features' in csv['metadata']['annotations']:
            infrastructure_features = csv['metadata']['annotations'][
                'operators.openshift.io/infrastructure-features'].lower()
            self.disconnected_supported = True if "disconnected" in infrastructure_features else False
            self.fips_supported = True if "fips" in infrastructure_features else False

        self.documentation_url = 'N/A'
        if 'links' in csv['spec']:
            for link in csv['spec']['links']:
                if link['name'].lower() == 'documentation':
                    self.documentation_url = link['url']

        self.repository = csv['metadata']['annotations']['repository'] if 'repository' in csv['metadata'][
            'annotations'] else None
        self.capabilities = csv['metadata']['annotations']['capabilities'] if 'capabilities' in csv['metadata'][
            'annotations'] else None

        logging.debug(f"info of {self.package_name} from {self.index.index_name} has been extracted.")

    def get_schema_list_json(self, package_path):
        schema_list = []
        with open(os.path.join(package_path, "catalog.json")) as operator_catalog_fp:
            schemelines = ""
            for jsonline in operator_catalog_fp:
                schemelines += jsonline
                if jsonline == '}\n':
                    scheme_json = json.loads(schemelines)
                    schema_list.append(scheme_json)
                    schemelines = ""
                    continue
        return schema_list

    def get_schema_list_yaml(self, package_path):
        schema_list = []
        with open(os.path.join(package_path, "catalog.yaml")) as operator_catalog_fp:
            try:
                parsed_yaml = yaml.safe_load_all(operator_catalog_fp)
                for schema in parsed_yaml:
                    schema_list.append(schema)
            except yaml.YAMLError as ex:
                logging.error(f"Couldn't parse {self.package_name} in {self.index.index_name}: {ex}")
        return schema_list


def get_loglevel():
    if LOGLEVEL == 'debug':
        loglevel = logging.DEBUG
    elif LOGLEVEL == 'info':
        loglevel = logging.INFO
    elif LOGLEVEL == 'warning':
        loglevel = logging.WARNING
    elif LOGLEVEL == 'error':
        loglevel = logging.ERROR
    elif LOGLEVEL == 'critical':
        loglevel = logging.CRITICAL
    else:
        loglevel = logging.INFO
    return loglevel


def main():
    index_images_names = [
        "redhat-operator-index",
        "community-operator-index",
        "certified-operator-index",
        "redhat-marketplace-index"
    ]
    op = OperatorsPoller(index_images_names)
    op.dump_jsons()
    logging.info("Finished.")


if __name__ == '__main__':
    main()
