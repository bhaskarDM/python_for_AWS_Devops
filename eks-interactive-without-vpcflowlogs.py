import networkx as nx
from pyvis.network import Network
from kubernetes import client, config
import boto3
import json
import os
from flask import Flask, request, render_template_string
from jinja2 import Template  # Ensure Jinja2 is used properly

app = Flask(__name__)

# Ensure static directory exists
if not os.path.exists("static"):
    os.makedirs("static")

def get_eks_clusters():
    """Retrieve the list of EKS clusters using boto3."""
    eks_client = boto3.client("eks")
    clusters = eks_client.list_clusters()["clusters"]
    return clusters

def get_k8s_namespaces():
    """Retrieve the list of namespaces in the selected cluster."""
    config.load_kube_config()
    v1 = client.CoreV1Api()
    namespaces = [ns.metadata.name for ns in v1.list_namespace().items]
    return namespaces

def get_k8s_resources(namespace=None):
    """Load Kubernetes config and retrieve nodes, pods, and services based on user selection."""
    config.load_kube_config()
    v1 = client.CoreV1Api()
    
    nodes = v1.list_node().items
    if namespace:
        pods = v1.list_namespaced_pod(namespace).items
        services = v1.list_namespaced_service(namespace).items
    else:
        pods = v1.list_pod_for_all_namespaces().items
        services = v1.list_service_for_all_namespaces().items
    
    return nodes, pods, services

def build_network_graph(nodes, pods, services):
    """Build a well-structured network graph of the EKS cluster."""
    G = nx.DiGraph()
    
    # Add Nodes (Kubernetes Nodes)
    for node in nodes:
        G.add_node(node.metadata.name, label=f'Node: {node.metadata.name}', color='blue', size=30)

    # Add Pods and link them to Nodes
    for pod in pods:
        pod_name = pod.metadata.name
        node_name = pod.spec.node_name
        G.add_node(pod_name, label=f'Pod: {pod_name}', color='green', size=20, title=f'Namespace: {pod.metadata.namespace}')
        if node_name:
            G.add_edge(node_name, pod_name, color="gray", title="Pod runs on Node")

    # Add Services and link them to Pods
    for svc in services:
        svc_name = svc.metadata.name
        G.add_node(svc_name, label=f'Service: {svc_name}', color='red', size=25, title=f'Namespace: {svc.metadata.namespace}')
        selector = svc.spec.selector
        if selector:
            for pod in pods:
                labels = pod.metadata.labels
                if labels and all(labels.get(k) == v for k, v in selector.items()):
                    G.add_edge(svc_name, pod.metadata.name, color="orange", title="Service routes to Pod")

    return G

@app.route('/')
def index():
    clusters = get_eks_clusters()
    namespaces = get_k8s_namespaces()
    
    html_template = """
    <html>
    <body>
        <h2>Select Cluster and Namespace</h2>
        <form action="/visualize" method="get">
            <label for="clusters">Cluster:</label>
            <select name="cluster">
                {% for cluster in clusters %}
                <option value="{{ cluster }}">{{ cluster }}</option>
                {% endfor %}
            </select>
            <label for="namespaces">Namespace:</label>
            <select name="namespace">
                {% for namespace in namespaces %}
                <option value="{{ namespace }}">{{ namespace }}</option>
                {% endfor %}
            </select>
            <button type="submit">Show Visualization</button>
        </form>
        <iframe id="vizFrame" width="100%" height="800px"></iframe>
    </body>
    </html>
    """
    return render_template_string(html_template, clusters=clusters, namespaces=namespaces)

@app.route('/visualize')
def visualize():
    cluster = request.args.get("cluster")
    namespace = request.args.get("namespace")
    nodes, pods, services = get_k8s_resources(namespace)
    G = build_network_graph(nodes, pods, services)
    
    output_file = f"static/eks_cluster_{cluster}_{namespace}.html"
    
    # Ensure the file path exists
    if not os.path.exists("static"):
        os.makedirs("static")
    
    net = Network(height="800px", width="100%", directed=True, notebook=False)
    
    for node, data in G.nodes(data=True):
        net.add_node(node, label=data.get("label", node), color=data.get("color", "gray"), size=data.get("size", 15), title=data.get("title", ""))
    
    for source, target, edge_data in G.edges(data=True):
        net.add_edge(source, target, color=edge_data.get("color", "gray"), title=edge_data.get("title", ""))

    # ✅ Manually define a Jinja2 template to prevent NoneType errors
    net.template = Template("""
        <html>
        <head>
            <script type="text/javascript" src="https://cdnjs.cloudflare.com/ajax/libs/vis/4.21.0/vis.min.js"></script>
        </head>
        <body>
            <div id="mynetwork" style="width:100%; height:800px;"></div>
            <script type="text/javascript">
                var nodes = new vis.DataSet({{ nodes|tojson }});
                var edges = new vis.DataSet({{ edges|tojson }});
                var container = document.getElementById('mynetwork');
                var data = {nodes: nodes, edges: edges};
                var options = { physics: { enabled: true } };
                var network = new vis.Network(container, data, options);
            </script>
        </body>
        </html>
    """)

    net.show(output_file)
    return f"<a href='/{output_file}'>Click here to view visualization</a>"

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
