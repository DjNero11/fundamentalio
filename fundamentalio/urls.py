from django.urls import path

from . import views

app_name = 'fundamentalio'

urlpatterns = [
    path('', views.home, name='home'),
    path('search/', views.search_page, name='search'),
    path('history/', views.history, name='history'),
    path('api/search/', views.search_api, name='api_search'),
    path('api/reports/start/', views.report_start_api, name='api_report_start'),
    path('api/reports/status/', views.report_status_api, name='api_report_status'),
    path('report/<uuid:id>/', views.report_detail, name='report_detail'),
]
