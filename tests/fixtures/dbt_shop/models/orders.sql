{{ config(
    materialized='incremental',
    schema='shop',
    partition_by={'field': 'order_date', 'data_type': 'date'}
) }}

select order_id, customer_id, order_date, status, total_amount
from {{ source('raw', 'orders') }}
