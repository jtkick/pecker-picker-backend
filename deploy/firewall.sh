#!/usr/bin/env bash
# Apply the stoke-prod Lightsail firewall. Run from your own machine (AWS CLI,
# signed in with `aws login`), e.g. after your home IP changes and SSH stops
# connecting. Replaces every rule with the set below.
#
#   deploy/firewall.sh              SSH from this machine's current public IP
#   deploy/firewall.sh 1.2.3.4      SSH from that IP instead
set -euo pipefail

instance=${INSTANCE:-stoke-prod}
ip=${1:-$(curl -fsS -m 10 https://checkip.amazonaws.com)}

aws lightsail put-instance-public-ports --instance-name "$instance" --port-infos \
    "fromPort=22,toPort=22,protocol=tcp,cidrs=$ip/32,cidrListAliases=lightsail-connect" \
    'fromPort=80,toPort=80,protocol=tcp,cidrs=0.0.0.0/0,ipv6Cidrs=::/0' \
    'fromPort=443,toPort=443,protocol=tcp,cidrs=0.0.0.0/0,ipv6Cidrs=::/0' \
    'fromPort=443,toPort=443,protocol=udp,cidrs=0.0.0.0/0,ipv6Cidrs=::/0' \
    --query 'operation.status' --output text

echo "SSH on $instance now allowed from $ip (plus the Lightsail browser terminal)."
