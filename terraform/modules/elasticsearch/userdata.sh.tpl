#!/bin/bash
set -euo pipefail

# Mount the data volume
mkfs -t xfs ${data_device} || true  # no-op if already formatted
mkdir -p ${data_mount}
mount ${data_device} ${data_mount}
echo "${data_device} ${data_mount} xfs defaults,nofail 0 2" >> /etc/fstab

# Install Elasticsearch
rpm --import https://artifacts.elastic.co/GPG-KEY-elasticsearch
cat > /etc/yum.repos.d/elasticsearch.repo << 'EOF'
[elasticsearch]
name=Elasticsearch repository for 8.x packages
baseurl=https://artifacts.elastic.co/packages/8.x/yum
gpgcheck=1
gpgkey=https://artifacts.elastic.co/GPG-KEY-elasticsearch
enabled=1
autorefresh=1
type=rpm-md
EOF

yum install -y elasticsearch-${es_version}

# Configure
cat > /etc/elasticsearch/elasticsearch.yml << EOF
cluster.name: ${cluster_name}
node.name: node-1
path.data: ${data_mount}
network.host: 0.0.0.0
discovery.type: single-node
xpack.security.enabled: false
EOF

# Set heap size
sed -i "s/-Xms[0-9]*[gGmM]/-Xms${heap_size}/" /etc/elasticsearch/jvm.options
sed -i "s/-Xmx[0-9]*[gGmM]/-Xmx${heap_size}/" /etc/elasticsearch/jvm.options

# Set data dir ownership and start
chown -R elasticsearch:elasticsearch ${data_mount}
systemctl enable elasticsearch
systemctl start elasticsearch
