{{ config(materialized='table', schema='shop', alias='shipments_v') }}

select shipment_id, order_id, customer_id, shipped_at, status, weight_kg
from {{ source('raw', 'shipments') }}
