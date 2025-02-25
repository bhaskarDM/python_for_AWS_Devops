import boto3
import pandas as pd
import ipaddress
from kubernetes import client, config
from diagrams import Diagram, Cluster
from diagrams.aws.compute import EC2
from diagrams.aws.database import RDS
from diagrams.aws.network import ELB, InternetGateway, NATGateway
from diagrams.k8s.compute import Pod
from diagrams.k8s.network import Service
from diagrams.k8s.group import Namespace


def get_eks_resources():
    """Fetch EKS Worker Nodes and related resources."""
    session = boto3.Session()
    ec2_client = session.client("ec2")

    resource_mapping = {}
    eks_nodes = set()

    ec2_instances = ec2_client.describe_instances()
    for reservation in ec2_instances["Reservations"]:
        for instance in reservation["Instances"]:
            private_ip = instance.get("PrivateIpAddress")
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
            node_group = tags.get("eks:nodegroup-name")

            if private_ip:
                if node_group:
                    eks_nodes.add(private_ip)
                    resource_mapping[private_ip] = f"EKS Worker ({tags.get('Name', instance['InstanceId'])})"
                else:
                    resource_mapping[private_ip] = f"EC2 ({tags.get('Name', instance['InstanceId'])})"

    print("✅ AWS Resources Detected:", resource_mapping)
    return resource_mapping, eks_nodes


def list_pods_from_k8s():
    """Fetch Kubernetes pods with their IPs, namespaces, and names."""
    try:
        config.load_kube_config()
    except:
        config.load_incluster_config()

    v1 = client.CoreV1Api()
    pods = v1.list_pod_for_all_namespaces(watch=False)

    pod_data = {
        pod.status.pod_ip: {"namespace": pod.metadata.namespace, "name": pod.metadata.name}
        for pod in pods.items if pod.status.pod_ip
    }

    print("✅ Kubernetes Pods Detected:", pod_data)
    return pod_data


def load_vpc_flow_logs(file_path, resource_mapping):
    """Load and filter VPC Flow Logs for EKS traffic."""

    columns = [
        "version", "account_id", "interface_id", "srcaddr", "pkt-srcaddr",
        "dstaddr", "pkt-dstaddr", "dstport", "protocol", "packets",
        "bytes", "start", "end", "action", "log_status",
    ]

    df = pd.read_csv(file_path, names=columns, skiprows=1)
    df = df.drop_duplicates(subset=["pkt-srcaddr", "pkt-dstaddr"])

    df = df[(df["pkt-srcaddr"].isin(resource_mapping) | df["pkt-dstaddr"].isin(resource_mapping))]

    print("✅ Sample VPC Flow Log Data:\n", df.head())
    return df


def create_png_diagram(df, resource_mapping, pod_mapping):
    """Generate a static PNG diagram using Diagrams with better layout, unique connections, and filtering."""
    
    # **Find all nodes that have at least one connection**
    connected_nodes = set(df["pkt-srcaddr"]).union(set(df["pkt-dstaddr"]))

    # **Filter resources to include only connected ones**
    resource_mapping = {ip: label for ip, label in resource_mapping.items() if ip in connected_nodes}
    pod_mapping = {ip: pod_info for ip, pod_info in pod_mapping.items() if ip in connected_nodes}

    with Diagram("EKS Architecture", show=False, outformat="png", filename="eks_architecture"):
        nodes = {}
        namespaces = {}

        # Cluster for AWS Services with extra padding
        with Cluster("AWS Services", direction="TB"):
            for ip, label in resource_mapping.items():
                if isinstance(label, dict):
                    label = str(label)  # Convert any unexpected dictionary values to a string

                if "EKS Worker" in label:
                    nodes[ip] = EC2(label)
                elif "RDS" in label:
                    nodes[ip] = RDS(label)
                elif "LoadBalancer" in label:
                    nodes[ip] = ELB(label)
                elif "NAT Gateway" in label:
                    nodes[ip] = NATGateway(label)
                elif "Internet Gateway" in label:
                    nodes[ip] = InternetGateway(label)
                else:
                    nodes[ip] = Service(label)  # Ensure label is a string

        # Cluster for Namespaces with more padding to avoid overlap
        namespace_clusters = {}
        for ip, pod_info in pod_mapping.items():
            ns = pod_info["namespace"]
            pod_label = f"{pod_info['name']}\n(ns: {ns})"  # Break into two lines to avoid overlap

            if ns not in namespace_clusters:
                namespace_clusters[ns] = Cluster(f"Namespace: {ns}", direction="TB")

            with namespace_clusters[ns]:
                nodes[ip] = Pod(pod_label)

        # **Store unique connections using a set**
        unique_connections = set()

        # Connect traffic flows
        for _, row in df.iterrows():
            src_ip = row["pkt-srcaddr"]
            dst_ip = row["pkt-dstaddr"]
            if src_ip in nodes and dst_ip in nodes:
                connection = (src_ip, dst_ip)  # Define a unique connection as a tuple
                if connection not in unique_connections:
                    nodes[src_ip] >> nodes[dst_ip]
                    unique_connections.add(connection)  # Add to set to prevent duplicates

    print("✅ PNG Diagram Generated: eks_architecture.png")


def main():
    file_path = "ekslogs.csv"  # Corrected CSV filename
    resource_mapping, eks_nodes = get_eks_resources()
    pod_mapping = list_pods_from_k8s()
    resource_mapping.update(pod_mapping)
    df = load_vpc_flow_logs(file_path, resource_mapping)
    create_png_diagram(df, resource_mapping, pod_mapping)


if __name__ == "__main__":
    main()
