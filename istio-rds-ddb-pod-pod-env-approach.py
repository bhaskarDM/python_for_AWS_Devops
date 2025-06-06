import subprocess
import json
import os
import signal
import time
import urllib.parse
from flask import Flask, send_file
import pyvis.network as net
import re

app = Flask(__name__)

def get_env_variables_for_service(service_name):
    """Fetches environment variables of a given service to identify AWS dependencies."""
    try:
        describe_cmd = f"kubectl get deployment {service_name} -n default -o json"
        describe_output = subprocess.run(describe_cmd, shell=True, capture_output=True, text=True)
        deployment_data = json.loads(describe_output.stdout)

        for container in deployment_data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            for env in container.get("env", []):
                if any(keyword in env.get("name", "") for keyword in ["RDS", "DB_HOST", "DATABASE_URL"]):
                    return env.get("value", "Unknown RDS")
                if any(keyword in env.get("name", "") for keyword in ["DYNAMODB", "TABLE"]):
                    return env.get("value", "Unknown DynamoDB Table")
                if any(keyword in env.get("name", "") for keyword in ["S3_BUCKET", "AWS_S3"]):
                    return env.get("value", "Unknown S3 Bucket")
                if any(keyword in env.get("name", "") for keyword in ["EC2_INSTANCE", "AWS_EC2"]):
                    return env.get("value", "Unknown EC2 Instance")
        return "Unknown AWS Service"
    except Exception as e:
        print(f"\U0001F6A8 Error fetching environment variables for {service_name}: {e}")
        return "Unknown AWS Service"

def get_service_graph():
    namespace = "default"

    port_forward_cmd = ["kubectl", "-n", "istio-system", "port-forward", "svc/prometheus", "9090:9090"]
    port_forward_process = subprocess.Popen(port_forward_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)

    try:
        time.sleep(5)
        prom_query = 'istio_requests_total{source_workload_namespace="default"}'
        encoded_query = urllib.parse.quote(prom_query)
        prom_cmd = f"curl -s 'http://localhost:9090/api/v1/query?query={encoded_query}'"
        prom_output = subprocess.run(prom_cmd, shell=True, capture_output=True, text=True)

        edges = set()
        nodes = set()
        aws_services = set()
        dynamodb_services = set()
        pod_communications = set()
        service_communications = set()

        if prom_output.stdout:
            prom_data = json.loads(prom_output.stdout)
            for result in prom_data.get('data', {}).get('result', []):
                src = result['metric'].get('source_workload', 'unknown')
                dest = result['metric'].get('destination_workload', 'unknown')
                dest_service = result['metric'].get('destination_service', 'unknown')
                
                if dest != "unknown" and src != "unknown":
                    pod_communications.add((src, dest))  # Capture pod-to-pod communication
                
                if dest_service != "unknown" and ".svc.cluster.local" in dest_service:
                    service_name = dest_service.split(".")[0]  # Extract service name
                    service_communications.add((src, service_name))
                    nodes.add(service_name)
                
                if re.match(r'169\.254\.[0-9]+\.[0-9]+', dest_service) or "amazonaws.com" in dest_service:
                    dest = get_env_variables_for_service(src)  # Fetch AWS service from source env vars
                    if "DynamoDB" in dest:
                        dynamodb_services.add(dest)
                    elif "S3" in dest:
                        aws_services.add(dest)
                    elif "EC2" in dest:
                        aws_services.add(dest)
                    else:
                        aws_services.add(dest)

                edges.add((src, dest))
                nodes.add(src)
                nodes.add(dest)
    finally:
        os.killpg(os.getpgid(port_forward_process.pid), signal.SIGTERM)

    return edges, nodes, aws_services, dynamodb_services, pod_communications, service_communications

def generate_interactive_graph(edges, nodes, aws_services, dynamodb_services, pod_communications, service_communications):
    G = net.Network(height="600px", width="100%", directed=True)
    G.force_atlas_2based(gravity=-30, central_gravity=0.001, spring_length=50, spring_strength=0.005, damping=0.6)
    
    if not nodes:
        print("\U0001F6A8 No nodes found, skipping graph generation.")
        return None

    print(f"Adding {len(nodes)} nodes and {len(edges)} edges to the graph.")

    for node in nodes:
        color = "lightblue"
        if node in aws_services:
            color = "orange"
        elif node in dynamodb_services:
            color = "green"
        G.add_node(node, label=node, color=color)

    for src, dest in edges:
        G.add_edge(src, dest)
    
    for src, dest in pod_communications:
        G.add_edge(src, dest, color='blue', title='Pod-to-Pod Communication')  # Highlight pod-to-pod communication
    
    for src, dest in service_communications:
        G.add_edge(src, dest, color='purple', title='Pod-to-Service Communication')  # Highlight pod-to-service communication

    G.set_options("""
    var options = {
      "physics": {
        "enabled": false
      },
      "manipulation": {
        "enabled": true
      },
      "nodes": {
        "fixed": {
          "x": false,
          "y": false
        }
      }
    }
    """)

    static_dir = "static"
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)

    graph_path = os.path.join(static_dir, "service_graph.html")

    try:
        print(f"Generating graph at {graph_path}...")
        G.write_html(graph_path)

        if not os.path.exists(graph_path):
            raise FileNotFoundError("Graph file was not created properly.")

        print("Graph successfully generated!")
        return graph_path
    except Exception as e:
        print(f"\U0001F6A8 Error generating graph: {e}")
    return None

@app.route('/')
def index():
    edges, nodes, aws_services, dynamodb_services, pod_communications, service_communications = get_service_graph()
    graph_path = generate_interactive_graph(edges, nodes, aws_services, dynamodb_services, pod_communications, service_communications)

    if not graph_path:
        return "Error: Graph rendering failed. Check logs.", 500

    return send_file(graph_path)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
