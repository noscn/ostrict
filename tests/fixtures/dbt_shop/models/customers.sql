{{ config(materialized='table', schema='shop') }}

select customer_id, region, user_email, signup_date
from {{ source('raw', 'customers') }}
