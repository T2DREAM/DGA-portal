import argparse
import datetime
import getpass
import re
import subprocess
import sys
import time

from base64 import b64encode
from os.path import expanduser

import boto3


class SpotClient(object):
    error_list = [
        'capacity-not-available',
        'capacity-oversubscribed',
        'not-scheduled-yet',
        'launch-group-constraint',
        'az-group-constraint',
        'placement-group-constraint',
        'constraint-not-fulfillable'
    ]
    instance_filters = [
        {
            'Name': 'availability-zone',
            'Values': [
                'us-west-2a',
                'us-west-2b',
                'us-west-2c'
            ],
        },
    ]

    def __init__(self, client, image_id, instance_type, security_groups):
        self.client = client
        self.image_id = image_id
        self.instance_type = instance_type
        self.security_groups = list(security_groups)
        self.spot_id = None


    def override_for_waiting(self, code_status):
        waiting = self.client.describe_spot_instance_requests(
            SpotInstanceRequestIds=[self.spot_id]
        )
        waiting_items_gen = (
            value
            for key, value in waiting.items()
            if key == 'SpotPriceHistory'
        )
        status_items_gen = (
            status_item
            for wait_item in waiting_items_gen
            for status_item in wait_item
            if status_item == 'Status'
        )
        for status_item in status_items_gen:
            for item in status_item:
                if item == 'Code':
                    code_status = item
        return code_status

    def get_instance_id(self):
        request = self.client.describe_spot_instance_requests(
            SpotInstanceRequestIds=[self.spot_id])
        instance_id = request['SpotInstanceRequests'][0]['InstanceId']
        # print("\n Instace ID: %s" % instance_id)
        return instance_id

    def get_spot_code(self):
        request = self.client.describe_spot_instance_requests(
            SpotInstanceRequestIds=[self.spot_id]
        )
        code_status_start = request['SpotInstanceRequests'][0]['Status']
        return code_status_start['Code']

    def wait_for_code_change(self):
        code_status = None
        while code_status != 'fulfilled':
            code_status = self.get_spot_code()
            if code_status == self.error_cleanup(code_status):
                exit()
            code_status = self.override_for_waiting(code_status)
            if code_status == 'price-too-low':
                print("Spot Instance ERROR: Bid placed is too low.")
                self.cancel_spot()
                exit()
            time.sleep(0.1)
            return code_status

    def tag_spot_instance(self, tag_data, elasticsearch, cluster_name):
        tags = [
            {'Key': 'Name', 'Value': tag_data['name']},
            {'Key': 'branch', 'Value': tag_data['branch']},
            {'Key': 'commit', 'Value': tag_data['commit']},
            {'Key': 'started_by', 'Value': tag_data['username']},
        ]
        if elasticsearch == 'yes':
            tags.append({'Key': 'elasticsearch', 'Value': elasticsearch})
        if cluster_name is not None:
            tags.append({'Key': 'ec_cluster_name', 'Value': cluster_name})
        instance_id = self.client.create_tags(
            Resources=[self.get_instance_id()],
            Tags=tags
        )
        return instance_id

    def error_cleanup(self, code_status):
        if code_status in self.error_list:
            print('Spot Instance ERROR: %s' % code_status)
            self.cancel_spot()
            exit()

    def cancel_spot(self):
        self.client.cancel_spot_instance_requests(SpotInstanceRequestIds=[self.spot_id])

    def spot_instance_price_check(self):
        todays_date = datetime.datetime.now()
        response = self.client.describe_spot_price_history(
            DryRun=False,
            StartTime=todays_date,
            EndTime=todays_date,
            InstanceTypes=[
                self.instance_type
            ],
            Filters=self.instance_filters
        )
        response_items_gen = (
            value
            for key, value in response.items()
            if key == 'SpotPriceHistory'
        )
        highest = 0
        for value in response_items_gen:
            for item in value:
                for i in item:
                    if i == 'SpotPrice':
                        print("SpotPrice: %s" % item[i])
                        if float(item[i]) > highest:
                            highest = float(item[i])
        print("Highest price: %f" % highest)
        return highest


def nameify(in_str):
    name = ''.join(
        c if c.isalnum() else '-'
        for c in in_str.lower()
    ).strip('-')
    return re.subn(r'\-+', '-', name)[0]


def _short_name(long_name):
    """
    Returns a short name for the branch name if found
    """
    if not long_name:
        return None
    regexes = [
        '(?:encd|sno)-[0-9]+',  # Demos
        '^v[0-9]+rc[0-9]+',     # RCs
        '^v[0-9]+x[0-9]+',      # Prod, Test
    ]
    result = long_name
    for regex_str in regexes:
        res = re.findall(regex_str, long_name, re.IGNORECASE)
        if res:
            result = res[0]
            break
    return result[:10].lower()


def tag_ec2_instance(instance, tag_data, elasticsearch, cluster_name):
    tags = [
        {'Key': 'Name', 'Value': tag_data['name']},
        {'Key': 'branch', 'Value': tag_data['branch']},
        {'Key': 'commit', 'Value': tag_data['commit']},
        {'Key': 'started_by', 'Value': tag_data['username']},
    ]
    if elasticsearch == 'yes':
        tags.append({'Key': 'elasticsearch', 'Value': elasticsearch})
    if cluster_name is not None:
        tags.append({'Key': 'ec_cluster_name', 'Value': cluster_name})
    instance.create_tags(Tags=tags)
    return instance


def read_ssh_key(identity_file):
    ssh_keygen_args = ['ssh-keygen', '-l', '-f', identity_file]
    fingerprint = subprocess.check_output(
        ssh_keygen_args
    ).decode('utf-8').strip()
    if fingerprint:
        with open(identity_file, 'r') as key_file:
            ssh_pub_key = key_file.readline().strip()
            return ssh_pub_key
    return None


def _get_bdm(main_args):
    return [
        {
            'DeviceName': '/dev/sda1',
            'Ebs': {
                'VolumeSize': int(main_args.volume_size),
                'VolumeType': 'gp2',
                'DeleteOnTermination': True
            }
        },
        {
            'DeviceName': '/dev/sdb',
            'NoDevice': "",
        },
        {
            'DeviceName': '/dev/sdc',
            'NoDevice': "",
        },
    ]


def get_user_data(commit, config_file, data_insert, main_args):
    cmd_list = ['git', 'show', commit + config_file]
    config_template = subprocess.check_output(cmd_list).decode('utf-8')
    ssh_pub_key = read_ssh_key(main_args.identity_file)
    if not ssh_pub_key:
        print(
            "WARNING: User is not authorized with ssh access to "
            "new instance because they have no ssh key"
        )
    data_insert['LOCAL_SSH_KEY'] = ssh_pub_key
    # aws s3 authorized_keys folder
    auth_base = 's3://t2depi-conf-prod/.aws/credentials'
    auth_type = 'prod'
    if main_args.profile_name != 'production':
        auth_type = 'demo'
    #auth_keys_dir = '{auth_base}/{auth_type}-authorized_keys'.format(
    #    auth_base=auth_base,
    #    auth_type=auth_type,
    #)
    data_insert['S3_AUTH_KEYS'] = auth_base
    data_insert['REDIS_PORT'] = main_args.redis_port
    user_data = config_template % data_insert
    return user_data


def _get_instances_tag_data(main_args):
    instances_tag_data = {
        'branch': main_args.branch,
        'commit': None,
        'short_name': _short_name(main_args.name),
        'name': main_args.name,
        'username': None,
    }
    if instances_tag_data['branch'] is None:
        instances_tag_data['branch'] = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD']
        ).decode('utf-8').strip()
    instances_tag_data['commit'] = subprocess.check_output(
        ['git', 'rev-parse', '--short', instances_tag_data['branch']]
    ).decode('utf-8').strip()
    if not subprocess.check_output(
            ['git', 'branch', '-r', '--contains', instances_tag_data['commit']]
        ).strip():
        print("Commit %r not in origin. Did you git push?" % instances_tag_data['commit'])
        sys.exit(1)
    instances_tag_data['username'] = getpass.getuser()
    if instances_tag_data['name'] is None:
        instances_tag_data['short_name'] = _short_name(instances_tag_data['branch'])
        instances_tag_data['name'] = nameify(
            '%s-%s-%s' % (
                instances_tag_data['short_name'],
                instances_tag_data['commit'],
                instances_tag_data['username'],
            )
        )
        if main_args.elasticsearch == 'yes':
            instances_tag_data['name'] = 'elasticsearch-' + instances_tag_data['name']
    return instances_tag_data


def _get_ec2_client(main_args, instances_tag_data):
    session = boto3.Session(region_name='us-west-2', profile_name=main_args.profile_name)
    ec2 = session.resource('ec2')
    if any(ec2.instances.filter(
            Filters=[
                {'Name': 'tag:Name', 'Values': [instances_tag_data['name']]},
                {'Name': 'instance-state-name',
                 'Values': ['pending', 'running', 'stopping', 'stopped']},
            ])):
        print('An instance already exists with name: %s' % instances_tag_data['name'])
        return None
    return ec2


def _get_run_args(main_args, instances_tag_data):
    master_user_data = None
    if not main_args.elasticsearch == 'yes':
        security_groups = ['ssh-http-https']
        #iam_role = 'encoded-instance'
        count = 1
        data_insert = {
            'WALE_S3_PREFIX': main_args.wale_s3_prefix,
            'COMMIT': instances_tag_data['commit'],
            'ROLE': main_args.role,
            'REGION_INDEX': 'False',
            'ES_IP': main_args.es_ip,
            'ES_PORT': main_args.es_port,
            'GIT_REPO': main_args.git_repo,
            'REDIS_IP': main_args.redis_ip,
            'REDIS_PORT': main_args.redis_port,
        }
        if main_args.no_es:
            config_file = ':cloud-config-no-es.yml'
        elif main_args.cluster_name:
            config_file = ':cloud-config-cluster.yml'
            data_insert['CLUSTER_NAME'] = main_args.cluster_name
            data_insert['REGION_INDEX'] = 'True'
        else:
            config_file = ':cloud-config.yml'
        if main_args.set_region_index_to:
            data_insert['REGION_INDEX'] = main_args.set_region_index_to
        user_data = get_user_data(instances_tag_data['commit'], config_file, data_insert, main_args)
    else:
        if not main_args.cluster_name:
            print("Cluster must have a name")
            sys.exit(1)
        count = int(main_args.cluster_size)
        security_groups = ['elasticsearch-https']
        #iam_role = 'elasticsearch-instance'
        config_file = ':cloud-config-elasticsearch.yml'
        data_insert = {
            'CLUSTER_NAME': main_args.cluster_name,
            'ES_DATA': 'true',
            'ES_MASTER': 'true',
            'MIN_MASTER_NODES': int(count/2 + 1),
            'GIT_REPO': main_args.git_repo,
        }
        if main_args.single_data_master:
            data_insert['ES_MASTER'] = 'false'
            data_insert['MIN_MASTER_NODES'] = 1
        user_data = get_user_data(instances_tag_data['commit'], config_file, data_insert, main_args)
        if main_args.single_data_master:
            master_data_insert = {
                'CLUSTER_NAME': main_args.cluster_name,
                'ES_DATA': 'false',
                'ES_MASTER': 'true',
                'MIN_MASTER_NODES': 1,
                'GIT_REPO': main_args.git_repo,
            }
            master_user_data = get_user_data(
                instances_tag_data['commit'],
                config_file,
                master_data_insert,
                main_args,
            )
    run_args = {
        'count': count,
        # 'iam_role': iam_role,
        'user_data': user_data,
        'security_groups': security_groups,
    }
    if master_user_data:
        run_args['master_user_data'] = master_user_data
    return run_args


def _get_instance_output(instances_tag_data, is_cluster_master=False):
    suffix = '-dm' if is_cluster_master else ''
    hostname = '{}.{}.encodedcc.org'.format(
        instances_tag_data['id'],
        instances_tag_data['domain'],
    )
    return [
        'Host %s%s.*' % (instances_tag_data['short_name'], suffix),
        '  Hostname %s' % hostname,
        '  # https://%s.demo.encodedcc.org' % instances_tag_data['name'],
        '  # ssh %s' % hostname,
        '  # %s' % instances_tag_data['id'],
    ]


def _wait_and_tag_instances(main_args, run_args, instances_tag_data, instances, cluster_master=False):
    tmp_name = instances_tag_data['name']
    instances_tag_data['domain'] = 'production' if main_args.profile_name == 'production' else 'instance'
    output_list = ['']
    is_cluster_master = False
    is_cluster = False
    if main_args.elasticsearch == 'yes' and run_args['count'] > 1:
        if cluster_master and run_args['master_user_data']:
            is_cluster_master = True
            output_list.append('Creating Elasticsearch Master Node for cluster')
        else:
            is_cluster = True
            output_list.append('Creating Elasticsearch cluster')
    created_cluster_master = False
    for i, instance in enumerate(instances):
        instances_tag_data['name'] = tmp_name
        instances_tag_data['id'] = instance.id
        if is_cluster_master:
            # Hack: current tmp_name was the last data cluster, so remove '4'
            instances_tag_data['name'] = "{}{}".format(tmp_name[0:-1], 'master')
        elif is_cluster:
            instances_tag_data['name'] = "{}{}".format(tmp_name, i)
        if not main_args.spot_instance:
            if is_cluster_master or (is_cluster and not created_cluster_master):
                created_cluster_master = True
                output_list.extend(_get_instance_output(
                    instances_tag_data,
                    is_cluster_master=is_cluster_master,
                ))
            elif is_cluster:
                output_list.append('  # %s' % instance.id)
            elif not is_cluster:
                output_list.extend(_get_instance_output(instances_tag_data))
            instance.wait_until_exists()
            tag_ec2_instance(instance, instances_tag_data, main_args.elasticsearch, main_args.cluster_name)
    for output in output_list:
        print(output)


def main():
    # Gather Info
    main_args = parse_args()
    instances_tag_data = _get_instances_tag_data(main_args)
    if instances_tag_data is None:
        sys.exit(10)
    ec2_client = _get_ec2_client(main_args, instances_tag_data)
    if ec2_client is None:
        sys.exit(20)
    run_args = _get_run_args(main_args, instances_tag_data)
    if main_args.dry_run_aws:
        print('Dry Run AWS')
        print('main_args', main_args)
        print('run_args', run_args.keys())
        print('Dry Run AWS')
        sys.exit(30)
    # Run Cases
    if main_args.check_price:
        print("check_price")
        boto_client = boto3.client('ec2')
        spot_client = SpotClient(
            boto_client,
            main_args.image_id,
            main_args.instance_type,
            run_args['security_groups']
        )
        spot_client.spot_instance_price_check()
    elif main_args.spot_instance:
        print("spot_instance")
        boto_client = boto3.client('ec2')
        # issue with base64 encoding so no decoding in utc-8 and recoding in base64 then decoding in base 64.
        user_config = subprocess.check_output(['git', 'show', instances_tag_data['commit'] + ':cloud-config.yml'])
        user_data_b64 = b64encode(user_config)
        run_args['user_data'] = user_data_b64.decode()
        spot_client = SpotClient(
            boto_client,
            main_args.image_id,
            main_args.instance_type,
            run_args['security_groups']
        )
        print("security_groups: %s" % run_args['security_groups'])
        bdm = _get_bdm(main_args)
        instances = spot_client.request_spot_instance(
            # run_args['iam_role'],
            main_args.spot_price,
            run_args['user_data'],
            bdm,
        )
        _wait_and_tag_instances(main_args, run_args, instances_tag_data, instances)
        spot_client.tag_spot_instance(instances_tag_data, main_args.elasticsearch, main_args.cluster_name)
        print("Spot instance request had been completed, please check to be sure it was fufilled")
    else:
        bdm = _get_bdm(main_args)
        instances = ec2_client.create_instances(
            ImageId=main_args.image_id,
            MinCount=run_args['count'],
            MaxCount=run_args['count'],
            InstanceType=main_args.instance_type,
            SecurityGroups=run_args['security_groups'],
            UserData=run_args['user_data'],
            BlockDeviceMappings=bdm,
            InstanceInitiatedShutdownBehavior='terminate',
            # IamInstanceProfile={
            #    "Name": run_args['iam_role'],
            #},
            Placement={
                'AvailabilityZone': main_args.availability_zone,
            },
        )
        _wait_and_tag_instances(main_args, run_args, instances_tag_data, instances)
        if 'master_user_data' in run_args and main_args.single_data_master:
            # ES MASTER instance when deploying elasticsearch data clusters
            if run_args['master_user_data'] and run_args['count'] > 1 and main_args.elasticsearch == 'yes':
                instances = ec2_client.create_instances(
                    ImageId='ami-2133bc59',
                    MinCount=1,
                    MaxCount=1,
                    InstanceType='c5.9xlarge',
                    SecurityGroups=['ssh-http-https'],
                    UserData=run_args['master_user_data'],
                    BlockDeviceMappings=bdm,
                    InstanceInitiatedShutdownBehavior='terminate',
                    IamInstanceProfile={
                        "Name": 'encoded-instance',
                    },
                    Placement={
                        'AvailabilityZone': main_args.availability_zone,
                    },
                )
                _wait_and_tag_instances(main_args, run_args, instances_tag_data, instances, cluster_master=True)


def parse_args():

    def check_region_index(value):
        lower_value = value.lower()
        allowed_values = [
            'true', 't',
            'false', 'f'
        ]
        if value.lower() not in allowed_values:
            raise argparse.ArgumentTypeError(
                "Noncase sensitive argument '%s' is not in [%s]." % (
                    str(value),
                    ', '.join(allowed_values),
                )
            )
        if lower_value[0] == 't':
            return 'True'
        return 'False'

    def check_volume_size(value):
        allowed_values = ['120', '200', '500']
        if not value.isdigit() or value not in allowed_values:
            raise argparse.ArgumentTypeError(
                "%s' is not in [%s]." % (
                    str(value),
                    ', '.join(allowed_values),
                )
            )
        return value

    def hostname(value):
        if value != nameify(value):
            raise argparse.ArgumentTypeError(
                "%r is an invalid hostname, only [a-z0-9] and hyphen allowed." % value)
        return value

    parser = argparse.ArgumentParser(
        description="Deploy DGA on AWS",
    )
    parser.add_argument(
        '-i',
        '--identity-file',
        default="{}/.ssh/id_rsa.pub".format(expanduser("~")),
        help="ssh identity file path"
    )
    parser.add_argument('-b', '--branch', default=None, help="Git branch or tag")
    parser.add_argument('-n', '--name', type=hostname, help="Instance name")
    parser.add_argument('--dry-run-aws', action='store_true', help="Abort before ec2 requests.")
    parser.add_argument('--single-data-master', action='store_true',
            help="Create a single data master node.")
    parser.add_argument('--check-price', action='store_true', help="Check price on spot instances")
    parser.add_argument('--cluster-name', default=None, help="Name of the cluster")
    parser.add_argument('--cluster-size', default=2, help="Elasticsearch cluster size")
    parser.add_argument('--elasticsearch', default=None, help="Launch an Elasticsearch instance")
    parser.add_argument('--es-ip', default='localhost', help="ES Master ip address")
    parser.add_argument('--es-port', default='9201', help="ES Master ip port")
    parser.add_argument('--image-id', default='ami-2133bc59',
                        help=(
                            "https://us-west-2.console.aws.amazon.com/ec2/home"
                            "?region=us-west-2#LaunchInstanceWizard:ami=ami-2133bc59"
                        ))
    parser.add_argument('--instance-type', default='c5.9xlarge',
                        help="c5.9xlarge for indexing. Switch to a smaller instance (m5.xlarge or c5.xlarge).")
    parser.add_argument('--profile-name', default=None, help="AWS creds profile")
    parser.add_argument('--no-es', action='store_true', help="Use non ES cloud condfig")
    parser.add_argument('--redis-ip', default='localhost', help="Redis IP.")
    parser.add_argument('--redis-port', default=6379, help="Redis Port.")
    parser.add_argument('--set-region-index-to', type=check_region_index,
                        help="Override region index in yaml to 'True' or 'False'")
    parser.add_argument('--spot-instance', action='store_true', help="Launch as spot instance")
    parser.add_argument('--spot-price', default='0.70', help="Set price or keep default price of 0.70")
    parser.add_argument('--teardown-cluster', default=None,
                        help="Takes down all the cluster launched from the branch")
    parser.add_argument('--volume-size', default=200, type=check_volume_size,
                        help="Size of disk. Allowed values 120, 200, and 500")
    parser.add_argument('--wale-s3-prefix', default='s3://t2depi-backups/production')
    parser.add_argument('--candidate', action='store_true', help="Deploy candidate instance")
    parser.add_argument('--release-candidate', action='store_true', help="Deploy release-candidate instance")
    parser.add_argument(
        '--test', action='store_const', default='demo', const='test', dest='role',
        help="Deploy to production AWS")
    parser.add_argument('--availability-zone', default='us-west-2a',
        help="Set EC2 availabilty zone")
    parser.add_argument('--git-repo', default='https://github.com/T2DREAM/dga-portal.git',
            help="Git repo to checkout branches: https://github.com/{user|org}/{repo}.git")
    # Set Role
    # - 'demo' role is default for making single or clustered
    # applications for feature building
    # - '--test' will set role to test
    # - 'rc' role is for Release-Candidate QA testing and
    # is the same as 'demo' except batchupgrade will be skipped during deployment.
    # This better mimics production but require a command be run after deployment.
    # - 'candidate' role is for production release that potential can
    # connect to produciton data.
    args = parser.parse_args()
    if not args.role == 'test':
        if args.release_candidate:
            args.role = 'rc'
            args.candidate = False
        elif args.candidate:
            args.role = 'candidate'
    return args


if __name__ == '__main__':
    main()