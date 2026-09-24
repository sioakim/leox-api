# OPNsense: management access beside PPPoE

This example is for a LEOX LXE-010X-A at `192.168.100.1/24`, connected to one
OPNsense Ethernet port. The same physical port carries untagged ONT management
traffic and VLAN 835 PPPoE traffic. Replace the example port, LAN address, and
API host address with your own values. Back up the OPNsense configuration before
changing interface assignments.

## 1. Keep PPPoE on VLAN 835

In **Interfaces → Devices → VLAN**, create or confirm VLAN tag **835** with the
physical ONT-facing port as its parent (for example `ix1`). In the PPPoE device
settings, select that VLAN device (for example `vlan01`) as the carrier. Keep
WAN assigned to the PPPoE interface (for example `pppoe1`). Your ISP's PPPoE
username and password belong only in OPNsense, not in this repository.

## 2. Assign the raw port for ONT management

In **Interfaces → Assignments**, add the physical parent port itself (for
example `ix1`) as another interface. Name it `ONT_MGMT`, enable it, and set:

| Setting | Value |
| --- | --- |
| IPv4 configuration | Static IPv4 |
| IPv4 address | `192.168.100.2/24` |
| IPv4 gateway | None |
| IPv6 configuration | None |

Apply the interface changes. The address is persistent because it is assigned
in OPNsense, rather than added with a temporary `ifconfig ... alias` command.
Do not move PPPoE onto the raw port; PPPoE continues over VLAN 835 on that same
cable. From OPNsense, verify that `192.168.100.1` responds on the management
network and that WAN remains connected through `pppoe1`.

## 3. Add a restricted HAProxy listener

Install and enable the OPNsense HAProxy plugin if needed. Under **Services →
HAProxy → Settings**, configure these objects (some OPNsense versions place
servers, backends, and frontends under **Real Servers**, **Backend Pools**, and
**Public Services**):

| Object | Setting | Value |
| --- | --- | --- |
| Real server `leox-mgmt` | Address and port | `192.168.100.1:80` (plain HTTP) |
| Backend `LEOXManagement` | Mode / servers | HTTP / `leox-mgmt` |
| Public service `LEOXManagementLAN` | Listen address | `<ROUTER_LAN_IP>:8100` (plain HTTP) |
| Public service `LEOXManagementLAN` | Mode | HTTP |
| Public service `LEOXManagementLAN` | Default backend | `LEOXManagement` |

Use a TCP health check or disable the health check if the ONT does not answer
the plugin's HTTP check method. Bind the public service to the router's **LAN
address**, not to all interfaces or the WAN. In the public service's **Advanced
pass-through / custom options**, add:

```haproxy
http-request deny unless { src <API_HOST_LAN_IP> }
http-request deny unless { req.hdr(X-Leox-API-Key) -m str <RANDOM_TOKEN> }
http-request del-header X-Leox-API-Key
```

Replace the angle-bracket values before saving. Generate a long hexadecimal
token, for example with `openssl rand -hex 32`,
keep it outside Git, and set the same value as `LEOX_PROXY_TOKEN` in the API
host's private `.env`. The header removal prevents the proxy token from being
forwarded to the ONT. Apply the HAProxy configuration and enable/start its
service. If your LAN firewall rules are restrictive, allow only the API host to
reach the router's LAN address on TCP 8100. OPNsense configuration exports
contain the token and must be stored privately.

The ONT has a single web login session per source IP. The proxy's source-IP and
token rules limit which LAN clients can share that session. The proxy handles
only HTTP management; it does not affect the PPPoE data path.

## 4. Point the API at the listener

On the API host, set `LEOX_BASE_URL=http://<ROUTER_LAN_IP>:8100`,
`LEOX_DEVICE_IP=192.168.100.1`, the ONT web login variables, and
`LEOX_PROXY_TOKEN` in `.env`. Set `LEOX_API_BIND_IP` to that host's LAN address
if a dashboard on another machine must call the API. Then run
`docker compose up -d --build`.

Verify in this order:

1. OPNsense WAN still has a PPPoE address and a default route through `pppoe1`.
2. From the API host, a request to the HAProxy listener without the token gets
   HTTP 403; with the token, the ONT login or status page is reachable.
3. `http://<API_HOST_LAN_IP>:8085/health` returns JSON health status, and
   `/pon` reports the ONT state (normally `O5` when registered).
4. A different LAN host cannot open the HAProxy listener. Confirm the interface
   assignment and HAProxy service remain enabled after an OPNsense reboot.

The ONT can emit a malformed login-required HTTP response; HAProxy may report
that as 502. The API recognizes that condition, logs in, and retries the status
read. If it cannot obtain a session, it returns a JSON 502 error.
