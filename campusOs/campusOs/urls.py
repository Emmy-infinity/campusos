# campusOs/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('campusos.urls')),   # lowercase app
]

admin.site.site_header = "campusOs Administration"
admin.site.site_title = "campusOs Admin"
admin.site.index_title = "Welcome to campusOs Control Panel"
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
