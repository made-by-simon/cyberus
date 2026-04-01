# SSH over USB-C to Jetson (Windows)

The Jetson exposes a virtual ethernet interface over USB-C at `192.168.55.1`.
Windows needs a static IP and correct route on the USB adapter to reach it.

## One-time setup (run as admin in PowerShell)

**1. Find the USB NCM adapter index:**
```powershell
Get-NetAdapter | Where-Object {$_.InterfaceDescription -like "*UsbNcm*"}
```
Note the `ifIndex` value (e.g. `4`).

**2. Disable DHCP and set a static IP:**
```powershell
Set-NetIPInterface -InterfaceIndex 4 -Dhcp Disabled
Remove-NetIPAddress -InterfaceIndex 4 -Confirm:$false
New-NetIPAddress -InterfaceIndex 4 -IPAddress 192.168.55.100 -PrefixLength 24
```

**3. Add a persistent route so traffic goes via the USB adapter:**
```powershell
route -p add 192.168.55.0 mask 255.255.255.0 0.0.0.0 IF 4
```

## Connect

```powershell
ssh cyberus@192.168.55.1
```

## If SSH stops working after a reconnect

The route may have been lost. Re-add it:
```powershell
route add 192.168.55.0 mask 255.255.255.0 0.0.0.0 IF 4
```

## Notes

- `192.168.55.1` is on the Jetson's `l4tbr0` USB gadget bridge — only reachable via USB-C, not WiFi
- WiFi SSH won't work if the network has client isolation enabled
- The USB adapter interface index is `4` on this machine — verify with step 1 if it changes
