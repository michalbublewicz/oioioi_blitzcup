from django import forms
from django.utils.translation import gettext_lazy as _

from oioioi.base.utils.validators import validate_db_string_id, validate_whitespaces


class GenerateMatchesForm(forms.Form):
    level_label = forms.CharField(
        label=_("Level label"),
        max_length=255,
        validators=[validate_whitespaces],
        help_text=_("Used in generated contest names, for example: Quarterfinals, Semifinals, Level 3."),
    )
    contest_id_prefix = forms.CharField(
        label=_("Contest ID prefix"),
        max_length=32,
        validators=[validate_db_string_id],
        help_text=_("Used as the prefix for generated contest IDs."),
    )
    xlsx_file = forms.FileField(
        label=_("Workbook"),
        help_text=_("Upload an .xlsx file with columns: match_code, player1_username, player2_username."),
    )

    def clean_xlsx_file(self):
        uploaded_file = self.cleaned_data["xlsx_file"]
        if not uploaded_file.name.lower().endswith(".xlsx"):
            raise forms.ValidationError(_("The uploaded file must have the .xlsx extension."))
        return uploaded_file
