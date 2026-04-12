from django.urls import path

from oioioi.blitz import views

contest_patterns = [
    path('blitz/status/', views.blitz_status_view, name='blitz_status'),
    path('blitz/generate-matches/', views.generate_matches_view, name='blitz_generate_matches'),
]
