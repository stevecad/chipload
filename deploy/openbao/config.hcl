# Local development: transport is HTTP on the private Compose network.
ui = true
disable_mlock = true
api_addr = "http://openbao:8200"
cluster_addr = "https://openbao:8201"

storage "raft" {
  path = "/openbao/file"
  node_id = "chipload-local"
}

audit "file" "file" {
  options {
    file_path = "/openbao/logs/audit.jsonl"
  }
}

listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable = true
}
