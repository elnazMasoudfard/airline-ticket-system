from django.urls import path

from . import views

app_name = 'tickets'

urlpatterns = [
    path('', views.ReservationListView.as_view(), name='reservation_list'),
    path('book/<int:seat_class_id>/', views.ReservationCreateView.as_view(), name='reservation_create'),
    path('book/<str:booking_reference>/passengers/', views.AddPassengersView.as_view(), name='add_passengers'),
    path('<str:booking_reference>/', views.ReservationDetailView.as_view(), name='reservation_detail'),
    path('<str:booking_reference>/cancel/', views.ReservationCancelView.as_view(), name='reservation_cancel'),
]