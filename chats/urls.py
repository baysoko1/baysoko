from django.urls import path
from . import views

urlpatterns = [
    path('', views.inbox, name='inbox'), 
    path('start/<int:listing_id>/<int:recipient_id>/', views.start_conversation, name='start-conversation'),
    path('api/grouped-conversations/', views.GroupedConversationsView.as_view(), name='grouped-conversations'),
    path('api/unread-messages-count/', views.UnreadMessagesCountView.as_view(), name='unread-messages-count'),
    path('api/get-online-users/', views.GetOnlineUsersView.as_view(), name='get-online-users'),
    path('api/online-users/', views.OnlineUsersListView.as_view(), name='online-users'),  # new endpoint
    path('api/search-users/', views.SearchUsersView.as_view(), name='search-users'),
    path('api/unified-conversation/<int:participant_id>/', views.UnifiedConversationView.as_view(), name='unified-conversation'),
    path('api/download-conversation-images/<int:participant_id>/', views.download_conversation_images, name='download-conversation-images'),
    path('api/send-unified-message/', views.SendUnifiedMessageView.as_view(), name='send-unified-message'),
    path('api/send-typing/<int:conversation_id>/', views.SendTypingView.as_view(), name='send-typing'),
    path('api/check-typing/<int:conversation_id>/', views.CheckTypingView.as_view(), name='check-typing'),
    path('api/edit-message/<int:message_id>/', views.EditMessageView.as_view(), name='edit-message'),
    path('api/pin-message/<int:message_id>/', views.PinMessageView.as_view(), name='pin-message'),
    path('api/delete-messages/', views.DeleteMessagesView.as_view(), name='delete-messages'),
    path('api/get-message-status/<int:conversation_id>/', views.GetMessageStatusView.as_view(), name='get-message-status'),
    path('api/archive-conversation/', views.ArchiveConversationView.as_view(), name='archive-conversation'),
    path('api/unarchive-conversation/', views.UnarchiveConversationView.as_view(), name='unarchive-conversation'),
    path('api/mute-conversation/<int:conversation_id>/', views.MuteConversationView.as_view(), name='mute-conversation'),
]