from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.DashboardHomeView.as_view(), name='home'),
    path('flights/', views.FlightManageListView.as_view(), name='flight_manage_list'),
    path('flights/new/', views.FlightCreateView.as_view(), name='flight_create'),
    path('flights/<int:pk>/edit/', views.FlightEditView.as_view(), name='flight_edit'),
    path('flights/<int:pk>/generate-seats/', views.GenerateSeatsView.as_view(), name='generate_seats'),
]