from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.services.android_phone_otp_reverification_recipe import (  # noqa: E402
    try_generate_android_phone_otp_reverification_recipe,
)
from app.services.codegen import CodeGenerator  # noqa: E402
from app.services.structural_edit import validate_kotlin_structure  # noqa: E402


PLAN = {
    "domain_playbook_id": "android_phone_otp_reverification",
    "objective": "Fix phone OTP re-verification.",
    "required_contracts": [
        {"contract_id": "customer_no_preverification_phone_write"},
        {"contract_id": "handyman_no_preverification_phone_write"},
    ],
}


CUSTOMER_SOURCE = """package com.example.handyman.customer_pages

fun CustomerKYCPhoneNumber() {
    val callbacks = object : PhoneAuthProvider.OnVerificationStateChangedCallbacks() {
        override fun onCodeSent(
            verificationId: String,
            token: PhoneAuthProvider.ForceResendingToken
        ) {
            isLoading = false
            val currentEmail = SessionManager.getLoggedInEmail(context)
            FirebaseDatabase.getInstance().getReference("User")
                .orderByChild("email").equalTo(currentEmail)
                .get().addOnSuccessListener { snapshot ->
                    for (child in snapshot.children) {
                        child.ref.child("phoneNumber").setValue(phoneNumber)
                    }
                    navController.navigate("customerKycCodeOTP/$verificationId/$phoneNumber")
                }.addOnFailureListener {
                    navController.navigate("customerKycCodeOTP/$verificationId/$phoneNumber")
                }
        }
    }
}
"""


HANDYMAN_SOURCE = """package com.example.handyman.handyman_pages

fun HandymanKYCPhoneNumber() {
    val callbacks = object : com.google.firebase.auth.PhoneAuthProvider.OnVerificationStateChangedCallbacks() {
        override fun onCodeSent(
            verificationId: String,
            token: com.google.firebase.auth.PhoneAuthProvider.ForceResendingToken
        ) {
            isLoading = false
            // Save phone number to database before navigating
            val currentEmail = SessionManager.getLoggedInEmail(context)
            val handymanRef = FirebaseDatabase.getInstance().getReference("Handyman")
            handymanRef.orderByChild("email").equalTo(currentEmail)
                .get().addOnSuccessListener { snapshot ->
                    for (child in snapshot.children) {
                        child.ref.child("phoneNumber").setValue(phoneNumber)
                    }
                    navController.navigate("handymanKycCodeOTP/$verificationId/$phoneNumber")
                }.addOnFailureListener {
                    navController.navigate("handymanKycCodeOTP/$verificationId/$phoneNumber")
                }
        }
    }
}
"""


CUSTOMER_CODE_SOURCE = """package com.example.handyman.customer_pages

fun CustomerKYCCodeOTP() {
    query.get().addOnSuccessListener { snapshot ->
        if (snapshot.exists()) {
            for (child in snapshot.children) {
                child.ref.child("isPhoneVerified").setValue(true)
                child.ref.child("status").setValue("Verified")
            }
        }
    }
    query.get().addOnSuccessListener { snapshot ->
        if (snapshot.exists()) {
            for (child in snapshot.children) {
                child.ref.child("isPhoneVerified").setValue(true)
                child.ref.child("status").setValue("Verified")
            }
        }
    }
}
"""


HANDYMAN_CODE_SOURCE = """package com.example.handyman.handyman_pages

fun HandymanKYCCodeOTP() {
    query.get().addOnSuccessListener { snapshot ->
        if (snapshot.exists()) {
            for (child in snapshot.children) {
                child.ref.child("isPhoneVerified").setValue(true)
                child.ref.child("verificationStatus").setValue("Verified")
            }
        }
    }
    query.get().addOnSuccessListener { snapshot ->
        if (snapshot.exists()) {
            for (child in snapshot.children) {
                child.ref.child("isPhoneVerified").setValue(true)
                child.ref.child("verificationStatus").setValue("Verified")
            }
        }
    }
}
"""


def test_recipe_removes_customer_preverification_db_block():
    result = try_generate_android_phone_otp_reverification_recipe(
        file_path=(
            "app/src/main/java/com/example/handyman/customer_pages/"
            "CustomerKYCPhoneNumber.kt"
        ),
        original_content=CUSTOMER_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert 'orderByChild("email").equalTo(currentEmail)' not in result.content
    assert 'child.ref.child("phoneNumber").setValue(phoneNumber)' not in result.content
    assert (
        'navController.navigate("customerKycCodeOTP/$verificationId/$phoneNumber")'
        in result.content
    )
    assert result.content.count("navController.navigate") == 1
    assert result.contract_coverage is not None
    assert result.contract_coverage["implemented_contracts"][0]["contract_id"] == (
        "customer_no_preverification_phone_write"
    )


def test_recipe_removes_handyman_preverification_db_block():
    result = try_generate_android_phone_otp_reverification_recipe(
        file_path=(
            "app/src/main/java/com/example/handyman/handyman_pages/"
            "HandymanKYCPhoneNumber.kt"
        ),
        original_content=HANDYMAN_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert 'orderByChild("email").equalTo(currentEmail)' not in result.content
    assert 'child.ref.child("phoneNumber").setValue(phoneNumber)' not in result.content
    assert (
        'navController.navigate("handymanKycCodeOTP/$verificationId/$phoneNumber")'
        in result.content
    )
    assert result.content.count("navController.navigate") == 1
    assert result.contract_coverage is not None
    assert result.contract_coverage["implemented_contracts"][0]["contract_id"] == (
        "handyman_no_preverification_phone_write"
    )


def test_recipe_adds_customer_postverification_phone_write():
    result = try_generate_android_phone_otp_reverification_recipe(
        file_path=(
            "app/src/main/java/com/example/handyman/customer_pages/"
            "CustomerKYCCodeOTP.kt"
        ),
        original_content=CUSTOMER_CODE_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert result.content.count('child.ref.child("phoneNumber").setValue(phoneNumber)') == 2
    assert (
        'child.ref.child("phoneNumber").setValue(phoneNumber)\n'
        '                child.ref.child("isPhoneVerified").setValue(true)'
        in result.content
    )
    assert result.contract_coverage is not None
    assert result.contract_coverage["implemented_contracts"][0]["contract_id"] == (
        "customer_postverification_phone_write"
    )


def test_recipe_adds_handyman_postverification_phone_write():
    result = try_generate_android_phone_otp_reverification_recipe(
        file_path=(
            "app/src/main/java/com/example/handyman/handyman_pages/"
            "HandymanKYCCodeOTP.kt"
        ),
        original_content=HANDYMAN_CODE_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert result.content.count('child.ref.child("phoneNumber").setValue(phoneNumber)') == 2
    assert result.contract_coverage is not None
    assert result.contract_coverage["implemented_contracts"][0]["contract_id"] == (
        "handyman_postverification_phone_write"
    )


def test_codegen_uses_phone_otp_recipe_before_provider_paths():
    settings = SimpleNamespace(
        codegen_agent_mode="static",
        codegen_provider="deepseek",
        primary_agent_provider="deepseek",
        codegen_output_format="auto",
        codegen_structural_kotlin_enabled=True,
        deepseek_api_key="test",
        deepseek_model="deepseek-test",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
    )
    generator = CodeGenerator(settings)
    generator._try_provider = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("provider path should not run for phone OTP recipe")
    )

    path = (
        "app/src/main/java/com/example/handyman/customer_pages/"
        "CustomerKYCPhoneNumber.kt"
    )
    result = generator.generate_patch(
        task_id="task-p69-21",
        plan_json={
            **PLAN,
            "must_touch_files": [path],
            "allowed_paths": [path],
        },
        context_files={path: CUSTOMER_SOURCE},
    )

    assert result.provider_name == "harness:android_phone_otp_reverification_recipe"
    assert result.model_name == "deterministic-v1"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.files_changed == [path]
    assert "-            val currentEmail = SessionManager.getLoggedInEmail(context)" in result.diff
    assert (
        '+            navController.navigate("customerKycCodeOTP/$verificationId/$phoneNumber")'
        in result.diff
    )
    assert result.contract_coverage is not None
