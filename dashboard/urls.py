from django.urls import path

from dashboard import views

urlpatterns = [
    path("", views.landing, name="marketing-landing"),
    path("app/", views.index, name="dashboard-index"),
    path("api/summary/", views.api_summary, name="dashboard-api-summary"),
    path("api/map/", views.api_map, name="dashboard-api-map"),
    path("api/zip/<str:zip_code>/", views.api_zip_apartments, name="dashboard-api-zip"),
    path(
        "api/apartment/<int:apartment_id>/units/",
        views.api_apartment_units,
        name="dashboard-api-units",
    ),
    path(
        "api/apartment/<int:apartment_id>/history/",
        views.api_unit_history,
        name="dashboard-api-history",
    ),
    path("api/price-alerts/", views.api_price_alerts, name="dashboard-api-price-alerts"),
    path(
        "api/price-alerts/<int:alert_id>/",
        views.api_price_alert_delete,
        name="dashboard-api-price-alert-delete",
    ),
]
