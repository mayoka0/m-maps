plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

dependencies {
    implementation("com.google.android.gms:play-services-location:21.3.0")
}

android {
    namespace = "org.mmaps.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.mmaps.companion"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    // Keep Java and Kotlin on the same target across different Android Studio JDK versions.
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
