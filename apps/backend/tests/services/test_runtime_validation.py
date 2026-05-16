from __future__ import annotations

from app.services.runtime_validation import validate_diff_semantics


def test_runtime_validation_ignores_incidental_titlecase_domain_labels() -> None:
    diff = """\
diff --git a/app/Phone.kt b/app/Phone.kt
--- a/app/Phone.kt
+++ b/app/Phone.kt
@@ -1,5 +1,3 @@
-val ref = FirebaseDatabase.getInstance().getReference("User")
-val handymanRef = FirebaseDatabase.getInstance().getReference("Handyman")
 nav()
"""
    context = {
        "app/Phone.kt": "",
        "app/Other.kt": 'getReference("User")\ngetReference("Handyman")\n',
    }

    report = validate_diff_semantics(
        diff,
        context,
        request_text="develop P69-21 phone OTP verification",
    )

    assert report.passed
    assert report.findings == []


def test_runtime_validation_still_flags_explicit_deleted_anchor() -> None:
    diff = """\
diff --git a/src/data/mockUsers.js b/src/data/mockUsers.js
--- a/src/data/mockUsers.js
+++ b/src/data/mockUsers.js
@@ -1,3 +1,2 @@
-  { id: "master1" },
   { id: "staff1" },
"""
    context = {
        "src/data/mockUsers.js": "",
        "src/pages/Dashboard.js": 'const id = "master1";\n',
    }

    report = validate_diff_semantics(
        diff,
        context,
        request_text='delete "master1" from mockUsers.js',
    )

    assert report.passed
    assert [finding.rule for finding in report.findings] == ["incomplete_replacement"]


def test_runtime_validation_allows_inline_case_normalized_comparison() -> None:
    diff = """\
diff --git a/src/pages/AdminSettings.js b/src/pages/AdminSettings.js
--- a/src/pages/AdminSettings.js
+++ b/src/pages/AdminSettings.js
@@ -1,2 +1,2 @@
+if (currentUser?.role?.toLowerCase() === "admin") {
+  showAdminTools();
+}
"""
    context = {
        "src/pages/AdminSettings.js": 'const roleOptions = ["Admin", "Staff"];\n',
    }

    report = validate_diff_semantics(diff, context)

    assert report.passed
    assert report.findings == []


def test_runtime_validation_allows_case_normalized_local_variable() -> None:
    diff = """\
diff --git a/src/pages/AdminSettings.js b/src/pages/AdminSettings.js
--- a/src/pages/AdminSettings.js
+++ b/src/pages/AdminSettings.js
@@ -1,2 +1,5 @@
+const normRole = admin.role?.toLowerCase();
+if (normRole === "master admin") return "Admin";
+if (normRole === "staff member") return "Staff";
"""
    context = {
        "src/pages/AdminSettings.js": 'const roleOptions = ["Master Admin", "Staff Member"];\n',
    }

    report = validate_diff_semantics(diff, context)

    assert report.passed
    assert report.findings == []


def test_runtime_validation_still_flags_raw_case_sensitive_comparison() -> None:
    diff = """\
diff --git a/src/pages/AdminSettings.js b/src/pages/AdminSettings.js
--- a/src/pages/AdminSettings.js
+++ b/src/pages/AdminSettings.js
@@ -1,2 +1,5 @@
+if (currentUser.role === "admin") showAdminTools();
"""
    context = {
        "src/pages/AdminSettings.js": 'const roleOptions = ["Admin", "Staff"];\n',
    }

    report = validate_diff_semantics(diff, context)

    assert report.passed
    assert [finding.rule for finding in report.findings] == ["case_sensitive_comparison"]
