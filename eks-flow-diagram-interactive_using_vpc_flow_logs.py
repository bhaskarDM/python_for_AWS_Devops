import boto3
import pandas as pd
import ipaddress
import json
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

    return resource_mapping, eks_nodes


def list_pods_from_k8s():
    """Fetch Kubernetes pods with their IPs, namespaces, and names."""
    try:
        config.load_kube_config()
    except:
        config.load_incluster_config()

    v1 = client.CoreV1Api()
    pods = v1.list_pod_for_all_namespaces(watch=False)

    return {
        pod.status.pod_ip: f"{pod.metadata.name} (ns: {pod.metadata.namespace})"
        for pod in pods.items if pod.status.pod_ip
    }


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

    return df


def create_interactive_html(df, resource_mapping):
    """Generate an interactive AWS + Kubernetes architecture HTML file using D3.js."""
    
    # **Find connected nodes**
    connected_nodes = set(df["pkt-srcaddr"]).union(set(df["pkt-dstaddr"]))

    # **Filter resources to include only connected ones**
    resource_mapping = {ip: label for ip, label in resource_mapping.items() if ip in connected_nodes}

    # **Detect bidirectional (two-way) connections**
    unique_connections = set()
    bidirectional_connections = set()

    for _, row in df.iterrows():
        src_ip = row["pkt-srcaddr"]
        dst_ip = row["pkt-dstaddr"]

        if src_ip in resource_mapping and dst_ip in resource_mapping:
            connection = (src_ip, dst_ip)
            reverse_connection = (dst_ip, src_ip)

            if reverse_connection in unique_connections:
                bidirectional_connections.add(connection)
                unique_connections.discard(reverse_connection)
            else:
                unique_connections.add(connection)

    # **Create nodes and links for the graph**
    nodes = [{"id": ip, "label": str(label)} for ip, label in resource_mapping.items()]
    links = [{"source": src, "target": dst, "bidirectional": False} for src, dst in unique_connections]
    links += [{"source": src, "target": dst, "bidirectional": True} for src, dst in bidirectional_connections]

    data_json = json.dumps({"nodes": nodes, "links": links}, indent=4)

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Interactive EKS Architecture</title>
        <script src="https://d3js.org/d3.v5.min.js"></script>
        <style>
            body {{ text-align: center; font-family: Arial; }}
            svg {{ width: 100%; height: 90vh; border: 1px solid black; }}
        </style>
    </head>
    <body>
        <h1>Interactive EKS Architecture</h1>
        <svg></svg>
        <script>
            var graph = {data_json};
            var width = window.innerWidth;
            var height = window.innerHeight * 0.85;

            var svg = d3.select("svg").attr("width", width).attr("height", height);
            var simulation = d3.forceSimulation(graph.nodes)
                .force("link", d3.forceLink(graph.links).id(d => d.id).distance(150))
                .force("charge", d3.forceManyBody().strength(-300))
                .force("center", d3.forceCenter(width / 2, height / 2));

            var link = svg.selectAll("line")
                .data(graph.links)
                .enter().append("line")
                .attr("stroke", d => d.bidirectional ? "red" : "gray")
                .attr("stroke-width", d => d.bidirectional ? 3 : 1.5)
                .attr("marker-end", "url(#arrow)");

            var node = svg.selectAll("g")
                .data(graph.nodes)
                .enter().append("g");

            node.append("circle")
                .attr("r", 15)
                .attr("fill", "blue");

            node.append("text")
                .attr("dx", 20)
                .attr("dy", ".35em")
                .text(d => d.label);

            simulation.nodes(graph.nodes).on("tick", () => {{
                link.attr("x1", d => d.source.x)
                    .attr("y1", d => d.source.y)
                    .attr("x2", d => d.target.x)
                    .attr("y2", d => d.target.y);
                node.attr("transform", d => `translate(${{d.x}}, ${{d.y}})`);
            }});
        </script>
    </body>
    </html>
    """

    with open("eks_interactive.html", "w") as f:
        f.write(html_content)

    print("✅ Interactive HTML Saved: eks_interactive.html")


def main():
    file_path = "ekslogs.csv"
    resource_mapping, eks_nodes = get_eks_resources()
    pod_mapping = list_pods_from_k8s()
    resource_mapping.update(pod_mapping)
    df = load_vpc_flow_logs(file_path, resource_mapping)
    create_interactive_html(df, resource_mapping)


if __name__ == "__main__":
    main()
