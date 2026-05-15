"""Cron / background scheduler tasks — split из server/scheduler.py.

Каждый модуль в этом пакете соответствует одному фоновому домену:
    creators.py — авто-prepare + publish постов креаторов

Обратная совместимость: re-export через server.scheduler.
"""
