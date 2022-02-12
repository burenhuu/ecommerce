# Generated by Django 2.2.26 on 2022-02-12 17:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0066_remove_account_microfrontend_url_field_from_SiteConfiguration'),
    ]

    operations = [
        migrations.CreateModel(
            name='BlackboardLearnerAssessmentDataTransmissionAudit',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('blackboard_user_email', models.CharField(max_length=255)),
                ('enterprise_course_enrollment_id', models.PositiveIntegerField(db_index=True)),
            ],
        ),
    ]
