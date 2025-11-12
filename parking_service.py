"""
Parking Management System - Web Service Module

This module implements the Flask web application with SocketIO for real-time communication.
It provides REST endpoints and WebSocket events for parking slot allocation and release,
receipt generation, and status updates. The service handles concurrent requests using
threading and provides comprehensive logging.

Key Features:
- WebSocket-based real-time updates for live UI synchronization
- REST API for status information
- Receipt generation for allocation and release
- Error handling and logging
- Thread-safe operations through the ParkingLot class

Endpoints:
- GET /: Main web interface
- WebSocket events: request_slot, release_slot
- GET /api/status: JSON status API

Dependencies:
- Flask: Web framework
- Flask-SocketIO: WebSocket support
- parking_models: Core business logic
"""

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import logging
from parking_models import *
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading')
parking_lot = ParkingLot()

# Configure logging to show timestamps, levels, and messages
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@app.route('/')
def index():
    """
    Main route serving the web interface.

    Returns:
        HTML: Rendered index.html template
    """
    return render_template('index.html')

@socketio.on('request_slot')
def handle_request_slot(data):
    """
    Handle WebSocket event for parking slot requests.

    Processes allocation requests, creates vehicles, validates entry policies,
    finds and allocates slots, generates receipts, and emits success/error responses.

    Expected data format from frontend:
    {
        'vehicle_type': 'small'|'medium'|'large',
        'customer_type': 'regular'|'vip',
        'is_ev': boolean,
        'license_plate': string,
        'entry_time': ISO string
    }

    Emits:
    - 'slot_allocated': Success with slot details and receipt
    - 'error': Failure with error message
    - 'status_update': Broadcast to all clients
    """
    logging.info(f"Received slot request: {data}")
    try:
        # Parse and validate input data from frontend
        vehicle_type_str = data['vehicle_type'].capitalize()  # 'small' -> 'Small'
        customer_type_str = data['customer_type'].capitalize()  # 'vip' -> 'Vip' -> 'VIP', 'regular' -> 'Regular'
        if customer_type_str == 'Vip':
            customer_type_str = 'VIP'
        is_ev = data.get('is_ev', False)
        license_plate = data.get('license_plate', '').strip()

        # If no license plate provided, generate one
        if not license_plate:
            license_plate = f"AUTO-{data.get('entry_time', 'NO-TIME')[:10].replace('-', '')}"

        logging.info(f"Parsed data - Vehicle: {vehicle_type_str}, Customer: {customer_type_str}, EV: {is_ev}, License: {license_plate}")

        vehicle_type = VehicleType(vehicle_type_str)
        customer_type = CustomerType(customer_type_str)

        # Create vehicle object
        vehicle = Vehicle(vehicle_type, customer_type, license_plate)

        # Handle VIP pass creation/renewal
        if customer_type == CustomerType.VIP:
            if (license_plate not in parking_lot.vip_passes or
                datetime.now() > parking_lot.vip_passes[license_plate]):
                # Create new VIP pass
                expiry = datetime.now() + timedelta(days=30)
                parking_lot.vip_passes[license_plate] = expiry
                vehicle.vip_pass_expiry = expiry
                logging.info(f"Created new VIP pass for {license_plate}, expires: {expiry}")
            else:
                # Use existing pass
                vehicle.vip_pass_expiry = parking_lot.vip_passes[license_plate]
                logging.info(f"Using existing VIP pass for {license_plate}, expires: {vehicle.vip_pass_expiry}")

        # Validate vehicle entry against policies
        can_enter, reason = parking_lot.validate_vehicle_entry(vehicle, is_ev)
        if not can_enter:
            logging.warning(f"Entry validation failed for {vehicle}: {reason}")
            emit('error', {'message': reason})
            return

        # Record re-entry if applicable
        if vehicle.re_entry_count > 0:
            vehicle.record_re_entry()

        logging.info(f"Created vehicle: {vehicle}")

        # Attempt to allocate a slot
        logging.info(f"Attempting allocation for {vehicle} (EV: {is_ev})")
        slot = parking_lot.allocate_slot(vehicle, is_ev)
        logging.info(f"Allocation result: {slot.id if slot else 'None'}")
        if slot:

            logging.info(f"Allocated slot {slot.id} with ticket {vehicle.ticket_id} for {vehicle} (EV: {is_ev})")

            # Generate allocation receipt
            receipt = generate_allocation_receipt(slot, vehicle, is_ev)

            # Emit success response to requesting client
            emit('slot_allocated', {
                'slot_id': slot.id,
                'ticket': vehicle.ticket_id,
                'level': slot.level,
                'vehicle_type': vehicle_type.value,
                'section': slot.section.value,
                'customer_type': customer_type.value,
                'license_plate': license_plate,
                'is_ev': is_ev,
                'allocation_time': slot.allocation_time.isoformat(),
                'receipt': receipt
            })

            # Broadcast status update to all connected clients
            emit_status()
        else:
            logging.warning(f"No slot available for {vehicle} (EV: {is_ev})")
            emit('error', {'message': 'No suitable slot available. Please try again later.'})

    except ValueError as e:
        logging.error(f"Invalid input data: {e}")
        emit('error', {'message': 'Invalid vehicle type or customer type provided.'})
    except KeyError as e:
        logging.error(f"Missing required field: {e}")
        emit('error', {'message': 'Missing required information. Please provide vehicle type and customer type.'})
    except Exception as e:
        logging.error(f"Unexpected error in slot request: {e}")
        emit('error', {'message': 'Internal server error. Please try again.'})

@socketio.on('release_slot')
def handle_release_slot(data):
    """
    Handle WebSocket event for parking slot release requests.

    Processes release requests by ticket ID, enforces policies, calculates fees,
    generates receipts, and emits success/error responses.

    Expected data format:
    {
        'ticket': 'ABC12345'
    }

    Emits:
    - 'released': Success with fee details and receipt
    - 'error': Failure with error message
    - 'status_update': Broadcast to all clients
    """
    ticket = data.get('ticket', '').strip()
    logging.info(f"Received release request for ticket: {ticket}")

    if not ticket:
        emit('error', {'message': 'Ticket ID is required.'})
        return

    try:
        # Process vehicle exit with policy enforcement
        exit_result = parking_lot.process_vehicle_exit(ticket)

        if not exit_result['success']:
            logging.warning(f"Exit processing failed for ticket {ticket}: {exit_result['reason']}")
            emit('error', {'message': exit_result['reason']})
            return

        vehicle = exit_result['vehicle']
        slot = exit_result['slot']
        base_fee = exit_result['base_fee']
        re_entry_fee = exit_result['re_entry_fee']
        total_fee = exit_result['total_fee']
        is_overstay = exit_result['overstay']
        warnings = exit_result['warnings']
        exit_time = exit_result['exit_time']

        duration = exit_time - slot.allocation_time
        hours = duration.total_seconds() / 3600

        logging.info(f"Successfully released ticket {ticket}, total fee: ₹{total_fee:.2f}, duration: {hours:.2f} hours")

        # Generate release receipt
        receipt = generate_release_receipt(slot, vehicle, base_fee, re_entry_fee, total_fee, hours, is_overstay, warnings)

        # Emit success response
        emit('released', {
            'ticket': ticket,
            'slot_id': slot.id,
            'base_fee': round(base_fee, 2),
            're_entry_fee': round(re_entry_fee, 2),
            'total_fee': round(total_fee, 2),
            'hours': round(hours, 2),
            'overstay': is_overstay,
            'warnings': warnings,
            'receipt': receipt
        })

        # Broadcast status update
        emit_status()

    except Exception as e:
        logging.error(f"Unexpected error in slot release: {e}")
        emit('error', {'message': 'Internal server error. Please try again.'})

def generate_allocation_receipt(slot, vehicle, is_ev=False):
    """
    Generate a receipt for slot allocation with comprehensive policy information.

    Args:
        slot (Slot): The allocated slot
        vehicle (Vehicle): The parked vehicle
        is_ev (bool): Whether this is an EV charging request

    Returns:
        dict: Receipt data for the client
    """
    time_limit_hours = ParkingRules.TIME_LIMITS[vehicle.customer_type]
    expiry_time = slot.allocation_time + timedelta(hours=time_limit_hours)

    if vehicle.customer_type == CustomerType.VIP:
        if vehicle.vip_pass_expiry:
            membership_fee = ParkingRules.MONTHLY_MEMBERSHIP_RATES[vehicle.vehicle_type]
            pricing_info = f"VIP Monthly Pass: ₹{membership_fee} (30 days unlimited parking)"
            time_limit = "Unlimited (VIP Pass)"
            expiry_time = vehicle.vip_pass_expiry
        else:
            membership_fee = ParkingRules.MONTHLY_MEMBERSHIP_RATES[vehicle.vehicle_type]
            pricing_info = f"VIP Monthly Pass: ₹{membership_fee} (30 days unlimited parking)"
            time_limit = "Unlimited (VIP Pass)"
            expiry_time = slot.allocation_time + timedelta(days=30)
    else:
        daily_rate = ParkingRules.DAILY_RATES[vehicle.vehicle_type]
        pricing_info = f"Daily Rate: ₹{daily_rate}"
        time_limit = f"{time_limit_hours} hours"

    return {
        'title': 'Parking Slot Allocation Receipt',
        'ticket': vehicle.ticket_id,
        'slot_id': slot.id,
        'level': slot.level,
        'section': slot.section.value,
        'vehicle_type': vehicle.vehicle_type.value,
        'customer_type': vehicle.customer_type.value,
        'license_plate': vehicle.license_plate,
        'is_ev': is_ev,
        'allocation_time': slot.allocation_time.strftime('%Y-%m-%d %H:%M:%S'),
        'expiry_time': expiry_time.strftime('%Y-%m-%d %H:%M:%S'),
        'time_limit': time_limit,
        'pricing_info': pricing_info,
        'daily_rate': f"₹{ParkingRules.DAILY_RATES[vehicle.vehicle_type]}",
        're_entry_fee': f"₹{ParkingRules.RE_ENTRY_RULES['re_entry_fee']}" if vehicle.re_entry_count > 0 else "₹0",
        'rules': ParkingRules.get_rules_text(),
        'qr_code': f"PARK-{vehicle.ticket_id}-{slot.allocation_time.strftime('%Y%m%d%H%M%S')}"
    }

def generate_release_receipt(slot, vehicle, base_fee, re_entry_fee, total_fee, hours, is_overstay, warnings):
    """
    Generate a receipt for slot release with detailed fee breakdown and policy information.

    Args:
        slot (Slot): The released slot
        vehicle (Vehicle): The released vehicle
        base_fee (float): Base parking fee
        re_entry_fee (float): Re-entry fee
        total_fee (float): Total fee
        hours (float): Parking duration in hours
        is_overstay (bool): Whether vehicle overstayed
        warnings (int): Number of warnings issued

    Returns:
        dict: Receipt data for the client
    """
    # Handle case where allocation_time might be None (shouldn't happen in normal flow)
    alloc_time_str = slot.allocation_time.strftime('%Y-%m-%d %H:%M:%S') if slot.allocation_time else 'Unknown'

    receipt = {
        'title': 'Parking Release Receipt',
        'ticket': vehicle.ticket_id,
        'slot_id': slot.id,
        'vehicle_type': vehicle.vehicle_type.value,
        'customer_type': vehicle.customer_type.value,
        'license_plate': vehicle.license_plate,
        'allocation_time': alloc_time_str,
        'release_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'duration_hours': round(hours, 2),
        'base_fee': f"₹{base_fee:.2f}",
        're_entry_fee': f"₹{re_entry_fee:.2f}",
        'total_fee': f"₹{total_fee:.2f}",
        'overstay': is_overstay,
        'warnings_issued': warnings,
        'rules': ParkingRules.get_rules_text(),
        'qr_code': f"RELEASE-{vehicle.ticket_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    }

    # Add VIP pass information
    if vehicle.vip_pass_expiry and datetime.now() < vehicle.vip_pass_expiry:
        receipt['vip_pass_info'] = f"VIP Pass active until {vehicle.vip_pass_expiry.strftime('%Y-%m-%d %H:%M:%S')} - No parking fee charged"
    elif vehicle.customer_type == CustomerType.VIP:
        receipt['vip_pass_info'] = "VIP Pass expired - Standard fees apply"

    if is_overstay:
        receipt['penalty_info'] = f"Overstay penalty: ₹{ParkingRules.RESTRICTIONS['penalty_per_hour']}/hour"

    if warnings > 0:
        receipt['warning_info'] = f"Warnings issued: {warnings}. Suspension after 3 warnings."

    return receipt

def emit_status():
    """
    Emit current parking lot status to all connected clients.

    Broadcasts slot availability counts, occupied slot details, and policy information.
    """
    occupied_slots = parking_lot.get_occupied_slots()
    all_slots = parking_lot.get_all_slots()

    # Build levels structure for visualization
    levels = {}
    for level in [1, 2]:
        levels[str(level)] = {}
        for vehicle_type in VehicleType:
            levels[str(level)][vehicle_type.value.lower()] = {}
            for section in Section:
                # Get slots for this level, vehicle_type, section
                section_slots = [slot for slot in all_slots
                               if slot.level == level and slot.vehicle_type == vehicle_type and slot.section == section]

                # Convert to dict format expected by frontend
                slots_data = []
                for slot in section_slots:
                    slot_data = {
                        'id': slot.id,
                        'occupied': slot.is_occupied,
                        'is_ev': slot.section == Section.EV,
                        'ticket': slot.vehicle.ticket_id if slot.is_occupied else None,
                        'remaining_time': None  # Could calculate if needed
                    }
                    slots_data.append(slot_data)

                levels[str(level)][vehicle_type.value.lower()][section.value.lower()] = {
                    'slots': slots_data
                }

    status = {
        'counters': {
            'total': len(all_slots),
            'occupied': len(occupied_slots),
            'available': len(all_slots) - len(occupied_slots),
            'expired': len(parking_lot.check_expired_slots())
        },
        'levels': levels,
        'rules': ParkingRules.get_rules_text(),
        'timestamp': datetime.now().isoformat()
    }
    socketio.emit('status_update', status)

@app.route('/api/status')
def api_status():
    """
    REST API endpoint for parking lot status.

    Returns comprehensive status including available slots, occupied slots,
    structured levels data, and policy information for visualization.

    Returns:
        JSON: Status data with counters, available slots, occupied slots, levels structure, and rules
    """
    occupied_slots = parking_lot.get_occupied_slots()
    all_slots = parking_lot.get_all_slots()

    # Build levels structure for visualization
    levels = {}
    for level in [1, 2]:
        levels[str(level)] = {}
        for vehicle_type in VehicleType:
            levels[str(level)][vehicle_type.value.lower()] = {}
            for section in Section:
                # Get slots for this level, vehicle_type, section
                section_slots = [slot for slot in all_slots
                               if slot.level == level and slot.vehicle_type == vehicle_type and slot.section == section]

                # Convert to dict format expected by frontend
                slots_data = []
                for slot in section_slots:
                    slot_data = {
                        'id': slot.id,
                        'occupied': slot.is_occupied,
                        'is_ev': slot.section == Section.EV,
                        'ticket': slot.vehicle.ticket_id if slot.is_occupied else None,
                        'remaining_time': None  # Could calculate if needed
                    }
                    slots_data.append(slot_data)

                levels[str(level)][vehicle_type.value.lower()][section.value.lower()] = {
                    'slots': slots_data
                }

    return jsonify({
        'counters': {
            'total': len(all_slots),
            'occupied': len(occupied_slots),
            'available': len(all_slots) - len(occupied_slots),
            'expired': len(parking_lot.check_expired_slots())
        },
        'available_slots': parking_lot.get_available_slots_count(),
        'occupied_slots': [
            {
                'slot_id': slot.id,
                'level': slot.level,
                'section': slot.section.value,
                'vehicle_type': slot.vehicle.vehicle_type.value,
                'customer_type': slot.vehicle.customer_type.value,
                'license_plate': slot.vehicle.license_plate,
                'ticket': slot.vehicle.ticket_id,
                'allocation_time': slot.allocation_time.isoformat() if slot.allocation_time else None
            }
            for slot in occupied_slots
        ],
        'levels': levels,
        'rules': ParkingRules.get_rules_text(),
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    logging.info("Starting Parking Management System on port 5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)