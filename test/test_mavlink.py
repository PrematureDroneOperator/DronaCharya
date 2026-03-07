"""
A simple script to test MAVLink connection and GPS reception directly on the Jetson Nano.
Run this on the Jetson to verify the Pixhawk is sending data over the serial port.
"""
import time
import sys
from pymavlink import mavutil

# Change this to match your baud rate config in Mission Planner / QGroundControl (likely 57600 or 921600)
SERIAL_PORT = "/dev/ttyTHS1"  
BAUD_RATE = 57600           

def main():
    print(f"Attempting to connect to MAVLink on {SERIAL_PORT} at {BAUD_RATE} baud...")
    
    try:
        # Create the connection
        master = mavutil.mavlink_connection(SERIAL_PORT, baud=BAUD_RATE)
        
        # Wait for the first heartbeat
        print("Waiting for heartbeat (this could take a few seconds)...")
        master.wait_heartbeat()
        print(f"✅ Heartbeat received from system (system {master.target_system} component {master.target_component})")
        
    except Exception as e:
        print(f"❌ Failed to connect or wait for heartbeat: {e}")
        print("\nFixes:")
        print("1. Check wiring (Jetson TX -> Pixhawk RX, Jetson RX -> Pixhawk TX, GND -> GND)")
        print("2. Ensure the serial port has correct permissions: sudo chmod 666 /dev/ttyTHS1")
        print("3. Ensure the baud rate matches the Pixhawk's SERIALx_BAUD parameter")
        sys.exit(1)

    print("\nListening for GPS messages (Press Ctrl+C to stop)...")
    
    # Request data stream (often needed if the Pixhawk is not configured to send data automatically)
    master.mav.request_data_stream_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 2, 1
    )

    try:
        while True:
            # Wait for a valid GPS message
            msg = master.recv_match(type=['GPS_RAW_INT', 'GLOBAL_POSITION_INT'], blocking=True, timeout=1.0)
            
            if not msg:
                # Print a dot to show we are still listening but no GPS msg arrived
                print(".", end="", flush=True)
                continue
                
            # If we got a GPS message, let's parse it
            if msg.get_type() == 'GPS_RAW_INT':
                fix_type = msg.fix_type
                # fix_type: 0-1: no fix, 2: 2D fix, 3: 3D fix, 4: DGPS, 5: RTK
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                alt = msg.alt / 1000.0  # in meters
                satellites = msg.satellites_visible
                
                status_emoji = "✅ 3D Fix" if fix_type >= 3 else "❌ No Fix"
                print(f"\n[GPS_RAW_INT] {status_emoji} | Lat: {lat:.6f}, Lon: {lon:.6f}, Alt: {alt:.1f}m | Sats: {satellites}")
                
            elif msg.get_type() == 'GLOBAL_POSITION_INT':
                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                # relative_alt is usually what we care about for drones (altitude above ground)
                rel_alt = msg.relative_alt / 1000.0 
                print(f"\n[GLOBAL_POSITION_INT] Lat: {lat:.6f}, Lon: {lon:.6f}, Rel Alt: {rel_alt:.1f}m")
                
            time.sleep(0.5) # Throttle console output

    except KeyboardInterrupt:
        print("\nStopping...")

if __name__ == "__main__":
    main()
