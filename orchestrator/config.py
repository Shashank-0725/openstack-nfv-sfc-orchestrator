import os

# ──────────────────────────────────────────────────────────────────────────────
# OpenStack Authentication
# ──────────────────────────────────────────────────────────────────────────────

AUTH = {
    'auth_url': os.environ.get(
        'OS_AUTH_URL',
        'http://10.10.10.10:5000'
    ),

    'username': os.environ.get(
        'OS_USERNAME',
        'admin'
    ),

    'password': os.environ.get(
        'OS_PASSWORD',
        ''
    ),

    'project_name': os.environ.get(
        'OS_PROJECT_NAME',
        'admin'
    ),

    'user_domain_name': os.environ.get(
        'OS_USER_DOMAIN_NAME',
        'Default'
    ),

    'project_domain_name': os.environ.get(
        'OS_PROJECT_DOMAIN_NAME',
        'Default'
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# SSH
# ──────────────────────────────────────────────────────────────────────────────

SSH_USER = 'ubuntu'

SSH_KEY = os.path.expanduser(
    '~/.ssh/id_rsa'
)

SSH_OPTS = (
    '-o StrictHostKeyChecking=no '
    '-o UserKnownHostsFile=/dev/null '
    '-o ConnectTimeout=10'
)


# ──────────────────────────────────────────────────────────────────────────────
# Images
# ──────────────────────────────────────────────────────────────────────────────

# Base Ubuntu image
IMAGE_NAME = 'ubuntu-22.04'

# Thin runtime image
# Contains:
#   - Docker
#   - IP forwarding enabled
#   - insecure registry config
#
# Actual VNFs run as containers.
VNF_THIN_IMAGE = 'vnf-thin-golden'

# Client/server images
CLIENT_IMAGE = '7c84ac67-cc41-44ae-98cb-943334a1da44'
SERVER_IMAGE = '3b3b5a70-fa5a-43d6-96c6-54014cce2243'


# ──────────────────────────────────────────────────────────────────────────────
# Container Registry
# ──────────────────────────────────────────────────────────────────────────────

REGISTRY = '192.168.137.184:5000'

VNF_CONTAINERS = {
    'firewall': f'{REGISTRY}/firewall-vnf:v1',
    'ids':      f'{REGISTRY}/ids-vnf:v1',
    'monitor':  f'{REGISTRY}/monitor-vnf:v1',
}


# ──────────────────────────────────────────────────────────────────────────────
# OpenStack Networking
# ──────────────────────────────────────────────────────────────────────────────

FLAVOR_NAME = 'test-flavor'

INT_NET = 'int-net'

EXT_NET = 'ext-net'


# ──────────────────────────────────────────────────────────────────────────────
# Compute Nodes
# ──────────────────────────────────────────────────────────────────────────────

COMPUTE_NODES = [
    'openstack-compute1',
    'openstack-compute2'
]


# ──────────────────────────────────────────────────────────────────────────────
# VNF Service Chain
# ──────────────────────────────────────────────────────────────────────────────

VNF_TYPES = [
    'firewall',
    'ids',
    'monitor'
]

CHAIN_ORDER = [
    'firewall',
    'ids',
    'monitor'
]


# ──────────────────────────────────────────────────────────────────────────────
# Port Naming
# ──────────────────────────────────────────────────────────────────────────────

PREFIX_MAP = {
    'firewall': 'fw',
    'ids':      'ids',
    'monitor':  'mon',
}


# ──────────────────────────────────────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────────────────────────────────────

FLASK_HOST = '0.0.0.0'

FLASK_PORT = 5050

POLL_INTERVAL = 5


# ──────────────────────────────────────────────────────────────────────────────
# Placement Heuristics
# ──────────────────────────────────────────────────────────────────────────────

# Estimated overhead per VNF
VNF_CPU_OVERHEAD = 15

VNF_RAM_OVERHEAD = 10

