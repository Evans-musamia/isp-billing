from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from strawberry.fastapi import GraphQLRouter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.graphql.schema import schema
from app.db.database import get_db
from app.db.models import Router, Customer, Plan, ProvisioningLog, ConnectionType, CustomerStatus, MpesaTransaction, MpesaTransactionStatus
from app.services.auth import verify_token, get_current_user
from app.services.billing import make_payment
from app.services.mikrotik_api import MikroTikAPI, validate_mac_address, normalize_mac_address
from app.services.mpesa_transactions import update_mpesa_transaction_status
from app.config import settings
import logging
from sqlalchemy.orm import selectinload
import json
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
import hashlib
from pprint import pformat

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ISP Billing SaaS API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Context getter for GraphQL
async def get_context(request: Request, db: AsyncSession = Depends(get_db)):
    auth_header = request.headers.get("Authorization")
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    return {
        "db": db,
        "user": token
    }




async def get_router_by_id(
    db: AsyncSession,
    router_id: int,
    user_id: int | None = None,
    role: str | None = None
) -> Router | None:
    stmt = select(Router).where(Router.id == router_id)
    if role != "admin" and user_id is not None:
        stmt = stmt.where(Router.user_id == user_id)
    res = await db.execute(stmt)
    return res.scalar_one_or_none()
# Mount GraphQL router
graphql_app = GraphQLRouter(schema, context_getter=get_context)
app.include_router(graphql_app, prefix="/graphql")



# @app.post("/api/lipay/callback")
# async def mpesa_callback(payload: dict, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
#     logger.info(f"--- M-Pesa Callback Received: {json.dumps(payload, indent=2)}")

#     # Extract values from the incoming payload
#     mac_address = payload.get("customer_ref")
#     status = payload.get("status")
#     amount = payload.get("amount")
#     tx_no = payload.get("lipay_tx_no")

#     logger.info(f"Parsed payload - mac_address: {mac_address}, status: {status}, amount: {amount}, tx_no: {tx_no}")

#     if not mac_address:
#         logger.error("Missing MAC Address in callback")
#         return {"ResultCode": 1, "ResultDesc": "Missing MAC Address"}

#     # Fetch customer with plan and router details
#     stmt = (
#         select(Customer)
#         .options(selectinload(Customer.plan), selectinload(Customer.router))
#         .where(Customer.mac_address == mac_address)
#     )
#     result = await db.execute(stmt)
#     customer = result.scalar_one_or_none()
#     logger.info(f"Customers found for MAC {mac_address}: {'1' if customer else '0'}")

#     if not customer:
#         logger.error(f"No customer found for MAC {mac_address}")
#         return {"ResultCode": 1, "ResultDesc": "Customer not found"}

#     if status == "completed":
#         logger.info(f"PAYMENT CONFIRMED for customer {customer.id} ({mac_address}). Checking pending_update_data...")
#         pending_update_data = customer.pending_update_data
#         logger.info(f"Raw pending_update_data for customer {customer.id}: {pending_update_data}")

#         now = datetime.utcnow()
#         plan = customer.plan
#         router = customer.router

#         if pending_update_data:
#             # Handle existing customer with pending update data
#             if isinstance(pending_update_data, str):
#                 try:
#                     pending_update_data = json.loads(pending_update_data)
#                     logger.info(f"Parsed pending_update_data for customer {customer.id}: {json.dumps(pending_update_data)}")
#                 except json.JSONDecodeError as e:
#                     logger.error(f"Invalid JSON in pending_update_data for customer {customer.id}: {e}")
#                     return {"ResultCode": 1, "ResultDesc": "Invalid pending update data format"}

#             duration_value = pending_update_data.get("duration_value")
#             duration_unit = pending_update_data.get("duration_unit")
#             applied_plan_id = pending_update_data.get("plan_id")
#             requested_router_id = pending_update_data.get("router_id")

#             if duration_value is None or duration_unit is None or requested_router_id is None:
#                 logger.error(f"Missing duration_value, duration_unit, or router_id in pending_update_data for customer {customer.id}: {json.dumps(pending_update_data)}")
#                 return {"ResultCode": 1, "ResultDesc": "Missing required fields in pending update data"}

#             # Convert duration to days or hours for MikroTik
#             if duration_unit.upper() == "DAYS":
#                 time_limit = f"{int(duration_value)}d"
#             elif duration_unit.upper() == "HOURS":
#                 time_limit = f"{int(duration_value)}h"
#             else:
#                 logger.error(f"Unsupported duration_unit {duration_unit} for customer {customer.id}")
#                 return {"ResultCode": 1, "ResultDesc": f"Unsupported duration unit: {duration_unit}"}

#             # Fetch the plan from pending_update_data
#             plan_stmt = select(Plan).where(Plan.id == applied_plan_id)
#             plan_result = await db.execute(plan_stmt)
#             plan = plan_result.scalar_one_or_none()

#             if not plan:
#                 logger.error(f"Pending plan_id {applied_plan_id} not found for customer {customer.id}")
#                 return {"ResultCode": 1, "ResultDesc": "Plan for extension not found"}

#             # Fetch the router from pending_update_data
#             router_stmt = select(Router).where(Router.id == requested_router_id)
#             router_result = await db.execute(router_stmt)
#             router = router_result.scalar_one_or_none()

#             if not router:
#                 logger.error(f"Router with id {requested_router_id} not found for customer {customer.id}")
#                 return {"ResultCode": 1, "ResultDesc": f"Router with id {requested_router_id} not found"}

#             # Calculate new expiry
#             if duration_unit.upper() == "DAYS":
#                 if customer.expiry and customer.expiry > now:
#                     new_expiry = customer.expiry + timedelta(days=int(duration_value))
#                     logger.info(f"Customer has unexpired time. Old expiry: {customer.expiry}, days to add: {duration_value}")
#                 else:
#                     new_expiry = now + timedelta(days=int(duration_value))
#                     logger.info(f"Customer has expired. Setting expiry from now: {now}, days: {duration_value}")
#             elif duration_unit.upper() == "HOURS":
#                 if customer.expiry and customer.expiry > now:
#                     new_expiry = customer.expiry + timedelta(hours=int(duration_value))
#                     logger.info(f"Customer has unexpired time. Old expiry: {customer.expiry}, hours to add: {duration_value}")
#                 else:
#                     new_expiry = now + timedelta(hours=int(duration_value))
#                     logger.info(f"Customer has expired. Setting expiry from now: {now}, hours: {duration_value}")

#             # Update customer
#             customer.expiry = new_expiry
#             customer.plan_id = applied_plan_id
#             customer.router_id = requested_router_id
#             customer.status = CustomerStatus.ACTIVE
#             customer.pending_update_data = None  # Clear pending data

#             logger.info(f"[AUDIT] Applied pending_update_data: {json.dumps(pending_update_data)}")
#             logger.info(f"[AUDIT] Customer {customer.id}: Plan set to {plan.name} ({plan.id}), expiry updated to {new_expiry}, router_id updated to {requested_router_id}")

#         else:
#             # Handle new customer (no pending_update_data)
#             if not plan or not router:
#                 logger.error(f"Customer {customer.id} missing plan or router configuration")
#                 return {"ResultCode": 1, "ResultDesc": "Customer missing plan or router configuration"}

#             # Use the customer's current plan duration
#             duration_value = plan.duration_value
#             duration_unit = plan.duration_unit.value

#             if duration_unit.upper() == "DAYS":
#                 time_limit = f"{int(duration_value)}d"
#                 new_expiry = now + timedelta(days=int(duration_value))
#                 logger.info(f"New customer: Setting expiry from now: {now}, days: {duration_value}")
#             elif duration_unit.upper() == "HOURS":
#                 time_limit = f"{int(duration_value)}h"
#                 new_expiry = now + timedelta(hours=int(duration_value))
#                 logger.info(f"New customer: Setting expiry from now: {now}, hours: {duration_value}")
#             else:
#                 logger.error(f"Unsupported duration_unit {duration_unit} for customer {customer.id}")
#                 return {"ResultCode": 1, "ResultDesc": f"Unsupported duration unit: {duration_unit}"}

#             # Update customer
#             customer.expiry = new_expiry
#             customer.status = CustomerStatus.ACTIVE

#             logger.info(f"[AUDIT] New customer {customer.id}: Plan set to {plan.name} ({plan.id}), expiry set to {new_expiry}")

#         # Commit customer updates
#         await db.commit()

#         # Log payment details
#         logger.info(f"[AUDIT] Payment success: {json.dumps(payload)}")
#         logger.info(f"[AUDIT] Customer {customer.id} new expiry: {customer.expiry}")

#         # Prepare payload for MikroTik provisioning
#         if router and plan:
#             hotspot_payload = {
#                 "mac_address": customer.mac_address,
#                 "username": customer.mac_address.replace(":", ""),
#                 "password": customer.mac_address.replace(":", ""),
#                 "time_limit": time_limit,
#                 "bandwidth_limit": f"{plan.speed}",
#                 "comment": f"Payment successful for {customer.name} on {datetime.utcnow().isoformat()}",
#                 "router_ip": router.ip_address,
#                 "router_username": router.username,
#                 "router_password": router.password,
#             }
#             logger.info(f"Prepared MikroTik Payload:\n{json.dumps(hotspot_payload, indent=2)}")
#             background_tasks.add_task(call_mikrotik_bypass, hotspot_payload)
#         else:
#             logger.error(f"Missing router or plan for customer {customer.id}")
#             return {"ResultCode": 1, "ResultDesc": "Missing router or plan configuration"}

#         return {
#             "ResultCode": 0,
#             "ResultDesc": f"Customer {customer.id} updated to ACTIVE and MikroTik user created. New expiry: {customer.expiry}"
#         }

#     elif status == "failed":
#         customer.status = CustomerStatus.INACTIVE
#         await db.commit()
#         logger.info(f"Customer {customer.id} status set to INACTIVE due to failed payment")
#         return {"ResultCode": 0, "ResultDesc": "Customer updated to INACTIVE"}

#     else:
#         logger.info(f"Payment status for customer {customer.id}: {status} (no action taken)")
#         return {"ResultCode": 0, "ResultDesc": f"No action taken for status: {status}"}
@app.post("/api/lipay/callback")
async def mpesa_callback(payload: dict, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    logger.info(f"--- M-Pesa Callback Received: {json.dumps(payload, indent=2)}")

    # Extract values from the incoming payload
    mac_address = payload.get("customer_ref")
    status = payload.get("status")
    amount = payload.get("amount")
    tx_no = payload.get("lipay_tx_no")
    checkout_request_id = payload.get("checkout_request_id")  # If available
    receipt_number = payload.get("receipt_number")  # If available

    logger.info(f"Parsed payload - mac_address: {mac_address}, status: {status}, amount: {amount}, tx_no: {tx_no}")

    if not mac_address:
        logger.error("Missing MAC Address in callback")
        return {"ResultCode": 1, "ResultDesc": "Missing MAC Address"}

    # Fetch customer with plan and router details
    stmt = (
        select(Customer)
        .options(selectinload(Customer.plan), selectinload(Customer.router))
        .where(Customer.mac_address == mac_address)
    )
    result = await db.execute(stmt)
    customer = result.scalar_one_or_none()
    logger.info(f"Customers found for MAC {mac_address}: {'1' if customer else '0'}")

    if not customer:
        logger.error(f"No customer found for MAC {mac_address}")
        return {"ResultCode": 1, "ResultDesc": "Customer not found"}

    if status == "completed":
        logger.info(f"PAYMENT CONFIRMED for customer {customer.id} ({mac_address})")
        
        # 🔥 NEW: Record the transaction in CustomerPayment table
        try:
            from app.services.reseller_payments import record_customer_payment
            from app.db.models import PaymentMethod
            
            # Get payment details from pending_update_data or current plan
            pending_update_data = customer.pending_update_data
            plan = customer.plan
            duration_value = None
            
            if pending_update_data:
                if isinstance(pending_update_data, str):
                    pending_update_data = json.loads(pending_update_data)
                duration_value = pending_update_data.get("duration_value")
                plan_id = pending_update_data.get("plan_id")
                
                # Fetch the plan if different
                if plan_id != customer.plan_id:
                    plan_stmt = select(Plan).where(Plan.id == plan_id)
                    plan_result = await db.execute(plan_stmt)
                    plan = plan_result.scalar_one_or_none()
            
            if not duration_value and plan:
                duration_value = plan.duration_value
            
            # Convert duration to days for payment record
            days_paid_for = duration_value
            if plan and plan.duration_unit.value.upper() == "HOURS":
                days_paid_for = max(1, duration_value // 24)  # Convert hours to days, minimum 1 day
            
            # Record the payment
            payment = await record_customer_payment(
                db=db,
                customer_id=customer.id,
                reseller_id=customer.user_id,  # Router owner/reseller
                amount=float(amount),
                payment_method=PaymentMethod.MOBILE_MONEY,
                days_paid_for=days_paid_for,
                payment_reference=receipt_number or tx_no,
                notes=f"M-Pesa payment via callback. TX: {tx_no}"
            )
            
            logger.info(f"[AUDIT] CustomerPayment record created: ID {payment.id}, Amount: {amount}, Days: {days_paid_for}")
            
        except Exception as payment_error:
            logger.error(f"Failed to record CustomerPayment for customer {customer.id}: {payment_error}")
            # Continue with customer update even if payment recording fails
        
        # 🔥 NEW: Update MpesaTransaction record if it exists
        if checkout_request_id:
            try:
                from app.services.mpesa_transactions import update_mpesa_transaction_status
                from app.db.models import MpesaTransactionStatus
                
                await update_mpesa_transaction_status(
                    db=db,
                    checkout_request_id=checkout_request_id,
                    status=MpesaTransactionStatus.COMPLETED,
                    receipt_number=receipt_number,
                    result_code="0",
                    result_desc="Payment completed successfully"
                )
                
                logger.info(f"[AUDIT] MpesaTransaction updated: {checkout_request_id}")
                
            except Exception as mpesa_error:
                logger.error(f"Failed to update MpesaTransaction {checkout_request_id}: {mpesa_error}")

        # Continue with existing customer update logic...
        pending_update_data = customer.pending_update_data
        now = datetime.utcnow()
        plan = customer.plan
        router = customer.router

        if pending_update_data:
            # Handle existing customer with pending update data
            if isinstance(pending_update_data, str):
                try:
                    pending_update_data = json.loads(pending_update_data)
                    logger.info(f"Parsed pending_update_data for customer {customer.id}: {json.dumps(pending_update_data)}")
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in pending_update_data for customer {customer.id}: {e}")
                    return {"ResultCode": 1, "ResultDesc": "Invalid pending update data format"}

            duration_value = pending_update_data.get("duration_value")
            duration_unit = pending_update_data.get("duration_unit")
            applied_plan_id = pending_update_data.get("plan_id")
            requested_router_id = pending_update_data.get("router_id")

            if duration_value is None or duration_unit is None or requested_router_id is None:
                logger.error(f"Missing duration_value, duration_unit, or router_id in pending_update_data for customer {customer.id}")
                return {"ResultCode": 1, "ResultDesc": "Missing required fields in pending update data"}

            # Convert duration to time limit for MikroTik
            if duration_unit.upper() == "DAYS":
                time_limit = f"{int(duration_value)}d"
            elif duration_unit.upper() == "HOURS":
                time_limit = f"{int(duration_value)}h"
            else:
                logger.error(f"Unsupported duration_unit {duration_unit} for customer {customer.id}")
                return {"ResultCode": 1, "ResultDesc": f"Unsupported duration unit: {duration_unit}"}

            # Fetch the plan and router from pending_update_data
            plan_stmt = select(Plan).where(Plan.id == applied_plan_id)
            plan_result = await db.execute(plan_stmt)
            plan = plan_result.scalar_one_or_none()

            router_stmt = select(Router).where(Router.id == requested_router_id)
            router_result = await db.execute(router_stmt)
            router = router_result.scalar_one_or_none()

            if not plan or not router:
                logger.error(f"Plan {applied_plan_id} or Router {requested_router_id} not found")
                return {"ResultCode": 1, "ResultDesc": "Plan or Router not found"}

            # Calculate new expiry
            if duration_unit.upper() == "DAYS":
                if customer.expiry and customer.expiry > now:
                    new_expiry = customer.expiry + timedelta(days=int(duration_value))
                else:
                    new_expiry = now + timedelta(days=int(duration_value))
            elif duration_unit.upper() == "HOURS":
                if customer.expiry and customer.expiry > now:
                    new_expiry = customer.expiry + timedelta(hours=int(duration_value))
                else:
                    new_expiry = now + timedelta(hours=int(duration_value))

            # Update customer
            customer.expiry = new_expiry
            customer.plan_id = applied_plan_id
            customer.router_id = requested_router_id
            customer.status = CustomerStatus.ACTIVE
            customer.pending_update_data = None  # Clear pending data

            logger.info(f"[AUDIT] Applied pending_update_data for customer {customer.id}")

        else:
            # Handle new customer (no pending_update_data)
            if not plan or not router:
                logger.error(f"Customer {customer.id} missing plan or router configuration")
                return {"ResultCode": 1, "ResultDesc": "Customer missing plan or router configuration"}

            duration_value = plan.duration_value
            duration_unit = plan.duration_unit.value

            if duration_unit.upper() == "DAYS":
                time_limit = f"{int(duration_value)}d"
                new_expiry = now + timedelta(days=int(duration_value))
            elif duration_unit.upper() == "HOURS":
                time_limit = f"{int(duration_value)}h"
                new_expiry = now + timedelta(hours=int(duration_value))
            else:
                logger.error(f"Unsupported duration_unit {duration_unit} for customer {customer.id}")
                return {"ResultCode": 1, "ResultDesc": f"Unsupported duration unit: {duration_unit}"}

            customer.expiry = new_expiry
            customer.status = CustomerStatus.ACTIVE
            logger.info(f"[AUDIT] New customer {customer.id} payment processed")

        # Commit customer updates
        await db.commit()

        # Prepare payload for MikroTik provisioning
        if router and plan:
            hotspot_payload = {
                "mac_address": customer.mac_address,
                "username": customer.mac_address.replace(":", ""),
                "password": customer.mac_address.replace(":", ""),
                "time_limit": time_limit,
                "bandwidth_limit": f"{plan.speed}",
                "comment": f"Payment successful for {customer.name} on {datetime.utcnow().isoformat()}",
                "router_ip": router.ip_address,
                "router_username": router.username,
                "router_password": router.password,
            }
            logger.info(f"Prepared MikroTik Payload for customer {customer.id}")
            background_tasks.add_task(call_mikrotik_bypass, hotspot_payload)

        return {
            "ResultCode": 0,
            "ResultDesc": f"Customer {customer.id} updated to ACTIVE, payment recorded, and MikroTik user created. New expiry: {customer.expiry}"
        }

    elif status == "failed":
        # 🔥 NEW: Update transaction records for failed payments
        if checkout_request_id:
            try:
                from app.services.mpesa_transactions import update_mpesa_transaction_status
                from app.db.models import MpesaTransactionStatus
                
                await update_mpesa_transaction_status(
                    db=db,
                    checkout_request_id=checkout_request_id,
                    status=MpesaTransactionStatus.FAILED,
                    result_code="1",
                    result_desc="Payment failed"
                )
                
                logger.info(f"[AUDIT] MpesaTransaction marked as failed: {checkout_request_id}")
                
            except Exception as mpesa_error:
                logger.error(f"Failed to update failed MpesaTransaction {checkout_request_id}: {mpesa_error}")
        
        customer.status = CustomerStatus.INACTIVE
        await db.commit()
        logger.info(f"Customer {customer.id} status set to INACTIVE due to failed payment")
        return {"ResultCode": 0, "ResultDesc": "Customer updated to INACTIVE, transaction marked as failed"}

    else:
        logger.info(f"Payment status for customer {customer.id}: {status} (no action taken)")
        return {"ResultCode": 0, "ResultDesc": f"No action taken for status: {status}"}
    
async def call_mikrotik_bypass(hotspot_payload: dict):
    try:
        api = MikroTikAPI(
            hotspot_payload["router_ip"],
            hotspot_payload["router_username"],
            hotspot_payload["router_password"]
        )

        if not api.connect():
            logger.error("Failed to connect to MikroTik router")
            return

        # 🔴 NO await here!
        result = api.add_customer_bypass_mode(
            hotspot_payload["mac_address"],
            hotspot_payload["username"],
            hotspot_payload["password"],
            hotspot_payload["time_limit"],
            hotspot_payload["bandwidth_limit"],
            hotspot_payload["comment"],
            hotspot_payload["router_ip"],
            hotspot_payload["router_username"],
            hotspot_payload["router_password"]
        )

        logger.info(f"MikroTik API Response: {json.dumps(result, indent=2)}")
        api.disconnect()
    except Exception as e:
        logger.error(f"Error while processing MikroTik bypass: {e}")

# MAC address registration endpoint (NO JWT REQUIRED - for guests)
@app.post("/api/clients/mac-register/{router_id}")
async def register_mac_address(
    router_id: int,
    registration: Dict[str, str],
    db: AsyncSession = Depends(get_db)
):
    """
    Register a MAC address for hotspot access.
    This endpoint is for guest users, so no authentication required.
    Router ID is used to associate the registration with the router owner.

    Expected payload:
    {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "time_limit": "24h" or "7d" (optional),
        "bandwidth_limit": "1M/2M" (optional)
    }
    """
    # First, verify the router exists and get its details
    router = await get_router_by_id(db, router_id)
    if not router:
        logger.warning(f"Registration attempt on non-existent router ID: {router_id}")
        raise HTTPException(status_code=404, detail="Router not found")

    # Validate MAC address
    mac_address = registration.get("mac_address")
    if not mac_address or not validate_mac_address(mac_address):
        logger.warning(f"Invalid MAC address format: {mac_address}")
        raise HTTPException(status_code=400, detail="Invalid MAC address format")

    normalized_mac = normalize_mac_address(mac_address)
    username = normalized_mac.replace(":", "")

    # Connect to the router
    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        logger.error(f"Failed to connect to router {router.name} ({router.ip_address})")
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # Check if MAC address is already registered
        existing_users = api.send_command("/ip/hotspot/user/print")
        if existing_users.get("success") and existing_users.get("data"):
            for user in existing_users["data"]:
                if user.get("name", "").upper() == username.upper():
                    logger.warning(f"MAC address {normalized_mac} already registered on router {router.name}")
                    raise HTTPException(status_code=409, detail="MAC address already registered")

        # Prepare user arguments
        args = {
            "name": username,
            "password": username,
            "profile": "default",
            "disabled": "no"
        }

        # Handle time limit if provided
        expires_at = None
        if registration.get("time_limit"):
            args["limit-uptime"] = registration["time_limit"]
            current_time = datetime.utcnow()
            time_limit = registration["time_limit"]

            if time_limit.endswith('h'):
                hours = int(time_limit[:-1])
                expires_at = current_time + timedelta(hours=hours)
            elif time_limit.endswith('d'):
                days = int(time_limit[:-1])
                expires_at = current_time + timedelta(days=days)

            # Add router owner info to comment for tracking
            comment = f"MAC: {normalized_mac} | Router: {router.name} | Owner: {router.user_id} | Guest"
            if expires_at:
                comment += f" | Expires: {expires_at.strftime('%Y-%m-%d %H:%M')}"
            args["comment"] = comment
        else:
            # Add router owner info even without time limit
            args["comment"] = f"MAC: {normalized_mac} | Router: {router.name} | Owner: {router.user_id} | Guest"

        # Create hotspot user
        result = api.send_command("/ip/hotspot/user/add", args)
        if "error" in result:
            logger.error(f"Failed to create hotspot user: {result['error']}")
            raise HTTPException(status_code=400, detail=result["error"])

        # Add IP binding for the MAC address
        binding_args = {
            "mac-address": normalized_mac,
            "type": "bypassed",
            "comment": f"Auto-registered: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} | Router: {router.name} | Guest"
        }
        binding_result = api.send_command("/ip/hotspot/ip-binding/add", binding_args)

        # Handle bandwidth limit and IP assignment if provided
        queue_result = None
        dhcp_lease_result = None
        assigned_ip = None

        if registration.get("bandwidth_limit"):
            # Generate a consistent IP based on MAC hash
            mac_hash = int(hashlib.md5(normalized_mac.encode()).hexdigest()[:4], 16)
            assigned_ip = f"192.168.1.{100 + (mac_hash % 150)}"

            # Add DHCP lease
            dhcp_lease_args = {
                "mac-address": normalized_mac,
                "address": assigned_ip,
                "server": "defconf",
                "comment": f"Auto-assigned for {username} | Router: {router.name} | Guest"
            }
            dhcp_lease_result = api.send_command("/ip/dhcp-server/lease/add", dhcp_lease_args)

            # Add queue rule if DHCP lease was successful
            if dhcp_lease_result.get("success") and "error" not in dhcp_lease_result:
                queue_args = {
                    "name": f"queue_{username}",
                    "target": f"{assigned_ip}/32",
                    "max-limit": registration["bandwidth_limit"],
                    "comment": f"Bandwidth limit for {normalized_mac} | Router: {router.name} | Guest"
                }
                queue_result = api.send_command("/queue/simple/add", queue_args)

                if "error" in queue_result:
                    logger.warning(f"Failed to set bandwidth limit: {queue_result['error']}")
                    # Remove DHCP lease if queue creation failed
                    if dhcp_lease_result.get("data") and len(dhcp_lease_result["data"]) > 0:
                        lease_id = dhcp_lease_result["data"][0].get(".id")
                        if lease_id:
                            api.send_command("/ip/dhcp-server/lease/remove", {"numbers": lease_id})

        # Log the registration for the router owner (for billing/tracking)
        logger.info(f"MAC {normalized_mac} registered on router {router.name} (ID: {router_id}, Owner: {router.user_id})")

        return {
            "success": True,
            "message": f"MAC address {normalized_mac} registered successfully",
            "user_details": {
                "username": username,
                "mac_address": normalized_mac,
                "router_id": router_id,
                "router_name": router.name,
                "router_owner_id": router.user_id,
                "registered_at": datetime.utcnow().isoformat(),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "bandwidth_limit": registration.get("bandwidth_limit"),
                "assigned_ip": assigned_ip,
                "binding_created": binding_result.get("success", False),
                "queue_created": queue_result.get("success", False) if queue_result else False
            }
        }

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"Unexpected error during MAC registration: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")
    finally:
        api.disconnect()

# Public router info endpoint (no auth required)
@app.get("/api/public/router/{router_id}")
async def get_public_router_info(
    router_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get basic router information for guest users.
    This can be used by captive portals to show router/ISP details.
    """
    router = await get_router_by_id(db, router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")

    # Return only public information
    return {
        "router_id": router.id,
        "router_name": router.name,
        "location": getattr(router, 'location', None),
        "isp_name": getattr(router, 'isp_name', None),
        "description": getattr(router, 'description', None),
        "contact_info": getattr(router, 'contact_info', None),
    }

# MAC registration status check (no auth required)
@app.get("/api/public/mac-status/{router_id}/{mac_address}")
async def check_mac_registration_status(
    router_id: int,
    mac_address: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Check if a MAC address is registered on a specific router.
    Useful for captive portals to determine user status.
    """
    if not validate_mac_address(mac_address):
        raise HTTPException(status_code=400, detail="Invalid MAC address format")

    router = await get_router_by_id(db, router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")

    normalized_mac = normalize_mac_address(mac_address)
    username = normalized_mac.replace(":", "")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # Check if user exists
        existing_users = api.send_command("/ip/hotspot/user/print")
        user_found = False
        user_details = None

        if existing_users.get("success") and existing_users.get("data"):
            for user in existing_users["data"]:
                if user.get("name", "").upper() == username.upper():
                    user_found = True
                    user_details = {
                        "registered": True,
                        "username": user.get("name"),
                        "disabled": user.get("disabled") == "true",
                        "profile": user.get("profile"),
                        "comment": user.get("comment", ""),
                        "mac_address": normalized_mac,
                        "router_id": router_id
                    }
                    break

        if not user_found:
            return {
                "registered": False,
                "mac_address": normalized_mac,
                "router_id": router_id
            }

        # Check for active sessions
        active_sessions = api.send_command("/ip/hotspot/active/print")
        is_active = False
        session_info = None

        if active_sessions.get("success") and active_sessions.get("data"):
            for session in active_sessions["data"]:
                if session.get("user") == username:
                    is_active = True
                    session_info = {
                        "login_time": session.get("login-time"),
                        "uptime": session.get("uptime"),
                        "bytes_in": session.get("bytes-in"),
                        "bytes_out": session.get("bytes-out"),
                        "address": session.get("address")
                    }
                    break

        user_details["active_session"] = is_active
        if session_info:
            user_details["session_info"] = session_info

        return user_details

    except Exception as e:
        logger.error(f"Error checking MAC status: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Status check failed: {str(e)}")
    finally:
        api.disconnect()

# Disconnect user endpoint (no auth required - for self-service)
@app.post("/api/public/disconnect/{router_id}/{mac_address}")
async def disconnect_user_session(
    router_id: int,
    mac_address: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Disconnect a user session. Can be used for self-service logout.
    """
    if not validate_mac_address(mac_address):
        raise HTTPException(status_code=400, detail="Invalid MAC address format")

    router = await get_router_by_id(db, router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")

    normalized_mac = normalize_mac_address(mac_address)
    username = normalized_mac.replace(":", "")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # Find and disconnect active sessions
        active_sessions = api.send_command("/ip/hotspot/active/print")
        disconnected_sessions = 0

        if active_sessions.get("success") and active_sessions.get("data"):
            for session in active_sessions["data"]:
                if session.get("user") == username:
                    session_id = session.get(".id")
                    if session_id:
                        disconnect_result = api.send_command("/ip/hotspot/active/remove", {"numbers": session_id})
                        if disconnect_result.get("success", True):  # Success if no error
                            disconnected_sessions += 1

        return {
            "success": True,
            "message": f"Disconnected {disconnected_sessions} session(s) for MAC {normalized_mac}",
            "mac_address": normalized_mac,
            "sessions_disconnected": disconnected_sessions
        }

    except Exception as e:
        logger.error(f"Error disconnecting user: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Disconnect failed: {str(e)}")
    finally:
        api.disconnect()

# Router status endpoint (requires auth)
@app.get("/api/routers")
async def get_routers(db: AsyncSession = Depends(get_db), token: str = Depends(verify_token)):
    """Get all routers for authenticated user"""
    user = await get_current_user(token, db)
    stmt = select(Router)
    if user.role != "admin":
        stmt = stmt.filter(Router.user_id == user.user_id)
    result = await db.execute(stmt)
    routers = result.scalars().all()
    return [{"id": r.id, "name": r.name, "ip_address": r.ip_address, "port": r.port} for r in routers]

# Get router users endpoint (requires auth)
@app.get("/api/routers/{router_id}/users")
async def get_router_users(
    router_id: int,
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    """Get all hotspot users for a specific router"""
    user = await get_current_user(token, db)
    router = await get_router_by_id(db, router_id, user.user_id, user.role)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found or not accessible")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        users_result = api.send_command("/ip/hotspot/user/print")
        active_sessions_result = api.send_command("/ip/hotspot/active/print")

        users = []
        active_sessions = {}

        # Build active sessions map
        if active_sessions_result.get("success") and active_sessions_result.get("data"):
            for session in active_sessions_result["data"]:
                username = session.get("user")
                if username:
                    active_sessions[username] = session

        # Build users list
        if users_result.get("success") and users_result.get("data"):
            for user in users_result["data"]:
                username = user.get("name", "")
                user_info = {
                    "username": username,
                    "profile": user.get("profile", ""),
                    "disabled": user.get("disabled") == "true",
                    "comment": user.get("comment", ""),
                    "uptime_limit": user.get("limit-uptime", ""),
                    "active": username in active_sessions
                }

                # Add session info if active
                if username in active_sessions:
                    session = active_sessions[username]
                    user_info["session"] = {
                        "address": session.get("address"),
                        "login_time": session.get("login-time"),
                        "uptime": session.get("uptime"),
                        "bytes_in": session.get("bytes-in"),
                        "bytes_out": session.get("bytes-out")
                    }

                users.append(user_info)

        return {
            "router_id": router_id,
            "router_name": router.name,
            "users": users,
            "total_users": len(users),
            "active_sessions": len(active_sessions)
        }

    except Exception as e:
        logger.error(f"Error getting router users: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get users: {str(e)}")
    finally:
        api.disconnect()




@app.delete("/api/routers/{router_id}/users/{username}")
async def remove_router_user(
    router_id: int,
    username: str,
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    """Remove a hotspot user from router"""
    user = await get_current_user(token, db)
    router = await get_router_by_id(db, router_id, user.user_id, user.role)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found or not accessible")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # First disconnect any active sessions
        active_sessions = api.send_command("/ip/hotspot/active/print")
        if active_sessions.get("success") and active_sessions.get("data"):
            for session in active_sessions["data"]:
                if session.get("user") == username:
                    session_id = session.get(".id")
                    if session_id:
                        api.send_command("/ip/hotspot/active/remove", {"numbers": session_id})

        # Remove the user
        users_result = api.send_command("/ip/hotspot/user/print")
        user_id = None

        if users_result.get("success") and users_result.get("data"):
            for user in users_result["data"]:
                if user.get("name") == username:
                    user_id = user.get(".id")
                    break

        if not user_id:
            raise HTTPException(status_code=404, detail="User not found")

        remove_result = api.send_command("/ip/hotspot/user/remove", {"numbers": user_id})

        if "error" in remove_result:
            raise HTTPException(status_code=400, detail=remove_result["error"])

        # Also remove IP bindings and queues if they exist
        # Convert username back to MAC format for cleanup
        if len(username) == 12 and username.isalnum():
            mac_address = ':'.join(username[i:i+2] for i in range(0, 12, 2))

            # Remove IP bindings
            bindings_result = api.send_command("/ip/hotspot/ip-binding/print")
            if bindings_result.get("success") and bindings_result.get("data"):
                for binding in bindings_result["data"]:
                    if binding.get("mac-address", "").upper() == mac_address.upper():
                        binding_id = binding.get(".id")
                        if binding_id:
                            api.send_command("/ip/hotspot/ip-binding/remove", {"numbers": binding_id})

            # Remove queues
            queues_result = api.send_command("/queue/simple/print")
            if queues_result.get("success") and queues_result.get("data"):
                for queue in queues_result["data"]:
                    if queue.get("name") == f"queue_{username}":
                        queue_id = queue.get(".id")
                        if queue_id:
                            api.send_command("/queue/simple/remove", {"numbers": queue_id})

        return {
            "success": True,
            "message": f"User {username} removed successfully",
            "username": username,
            "router_id": router_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing user: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to remove user: {str(e)}")
    finally:
        api.disconnect()

# Router stats endpoint (requires auth)
@app.get("/api/router_stats/{router_id}")
async def get_router_stats(
    router_id: int,
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    """Get router statistics and active users"""
    user = await get_current_user(token, db)
    router = await get_router_by_id(db, router_id, user.user_id, user.role)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found or not accessible")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # Get hotspot users
        users_result = api.send_command("/ip/hotspot/user/print")
        total_users = 0
        if users_result.get("success") and users_result.get("data"):
            total_users = len(users_result["data"])

        # Get active sessions
        active_sessions_result = api.send_command("/ip/hotspot/active/print")
        active_sessions = 0
        active_users = []

        if active_sessions_result.get("success") and active_sessions_result.get("data"):
            active_sessions = len(active_sessions_result["data"])
            for session in active_sessions_result["data"]:
                active_users.append({
                    "username": session.get("user"),
                    "address": session.get("address"),
                    "login_time": session.get("login-time"),
                    "uptime": session.get("uptime"),
                    "bytes_in": session.get("bytes-in"),
                    "bytes_out": session.get("bytes-out")
                })

        # Get router system info
        system_result = api.send_command("/system/resource/print")
        system_info = {}
        if system_result.get("success") and system_result.get("data"):
            data = system_result["data"][0] if system_result["data"] else {}
            system_info = {
                "cpu_load": data.get("cpu-load"),
                "uptime": data.get("uptime"),
                "free_memory": data.get("free-memory"),
                "total_memory": data.get("total-memory"),
                "version": data.get("version"),
                "board_name": data.get("board-name")
            }

        return {
            "router_id": router_id,
            "router_name": router.name,
            "total_users": total_users,
            "active_sessions": active_sessions,
            "active_users": active_users,
            "system_info": system_info,
            "last_updated": datetime.utcnow().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting router stats: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get router stats: {str(e)}")
    finally:
        api.disconnect()

# Sync router users endpoint (requires auth)
@app.post("/api/routers/{router_id}/sync")
async def sync_router_users_with_database(
    router_id: int,
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    """Sync router users with database customers"""
    user = await get_current_user(token, db)
    router = await get_router_by_id(db, router_id, user.user_id, user.role)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found or not accessible")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # Get all users from router
        users_result = api.send_command("/ip/hotspot/user/print")
        router_users = []
        if users_result.get("success") and users_result.get("data"):
            router_users = users_result["data"]

        # Get customers assigned to this router
        customers_result = await db.execute(
            select(Customer).where(Customer.router_id == router_id)
        )
        db_customers = customers_result.scalars().all()

        sync_report = {
            "router_users": len(router_users),
            "db_customers": len(db_customers),
            "synced": 0,
            "errors": []
        }

        # Create sets for comparison
        router_usernames = {user.get("name", "").lower() for user in router_users}
        db_usernames = {customer.username.lower() for customer in db_customers if customer.username}

        # Find mismatches
        only_in_router = router_usernames - db_usernames
        only_in_db = db_usernames - router_usernames

        sync_report["only_in_router"] = list(only_in_router)
        sync_report["only_in_db"] = list(only_in_db)
        sync_report["synced"] = len(router_usernames & db_usernames)

        return sync_report

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing router users: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")
    finally:
        api.disconnect()

@app.delete("/api/public/remove-bypassed/{router_id}/{mac_address}")
async def remove_bypassed_user_public(
    router_id: int,
    mac_address: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Fully remove (hotspot user + bindings + queues + dhcp lease)
    for the given MAC address on the given router.
    No JWT required.
    """
    if not validate_mac_address(mac_address):
        raise HTTPException(status_code=400, detail="Invalid MAC address format")

    normalized_mac = normalize_mac_address(mac_address)
    username = normalized_mac.replace(":", "")

    router = await get_router_by_id(db, router_id)
    if not router:
        raise HTTPException(status_code=404, detail="Router not found")

    api = MikroTikAPI(router.ip_address, router.username, router.password, router.port)
    if not api.connect():
        raise HTTPException(status_code=500, detail="Failed to connect to router")

    try:
        # Disconnect active sessions
        active_sessions = api.send_command("/ip/hotspot/active/print")
        if active_sessions.get("success") and active_sessions.get("data"):
            for session in active_sessions["data"]:
                if session.get("user") == username:
                    sid = session.get(".id")
                    if sid:
                        api.send_command("/ip/hotspot/active/remove", {"numbers": sid})

        # Remove hotspot user
        users = api.send_command("/ip/hotspot/user/print")
        uid = None
        if users.get("success") and users.get("data"):
            for u in users["data"]:
                if u.get("name") == username:
                    uid = u.get(".id")
                    break
        if uid:
            api.send_command("/ip/hotspot/user/remove", {"numbers": uid})

        # Clean up IP bindings, queues, DHCP lease
        bindings = api.send_command("/ip/hotspot/ip-binding/print")
        if bindings.get("success") and bindings.get("data"):
            for b in bindings["data"]:
                if b.get("mac-address", "").upper() == normalized_mac.upper():
                    api.send_command("/ip/hotspot/ip-binding/remove", {"numbers": b[".id"]})

        queues = api.send_command("/queue/simple/print")
        if queues.get("success") and queues.get("data"):
            for q in queues["data"]:
                if q.get("name") == f"queue_{username}":
                    api.send_command("/queue/simple/remove", {"numbers": q[".id"]})

        leases = api.send_command("/ip/dhcp-server/lease/print")
        if leases.get("success") and leases.get("data"):
            for l in leases["data"]:
                if l.get("mac-address", "").upper() == normalized_mac.upper():
                    api.send_command("/ip/dhcp-server/lease/remove", {"numbers": l[".id"]})

        return {
            "success": True,
            "message": f"User with MAC {normalized_mac} removed successfully",
            "mac_address": normalized_mac,
            "router_id": router_id
        }

    except Exception as e:
        logger.error(f"Error removing bypassed user {normalized_mac}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        api.disconnect()

@app.get("/")
def read_root():
    return {"message": "ISP Billing SaaS API", "version": "1.0.0"}

@app.get("/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8008, reload=False)