from netmiko import ConnectHandler

device = {
    "device_type": "cisco_ios",
    "host": "192.168.0.242",
    "username": "admin",
    "password": "cisco123",
    "secret": "cisco123",
}

fix_commands = [
    "router bgp 65030",
    "neighbor 10.12.0.1 remote-as 65010",
]

conn = ConnectHandler(**device)
conn.enable()
output = conn.send_config_set(fix_commands)
print(output)
conn.disconnect()