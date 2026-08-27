from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('register/', views.RegisterView.as_view(), name='register'),
    path('verify-email/<str:token>/', views.VerifyEmailView.as_view(), name='verify_email'),
    path('resend-verification-email/', views.ResendVerificationEmailView.as_view(), name='resend_verification_email'),
    path('verify-phone/', views.RequestPhoneVerificationView.as_view(), name='verify_phone'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('profile/', views.ProfileView.as_view(), name='profile'),
    path('wallet/deposit/', views.DepositView.as_view(), name='deposit'),
]