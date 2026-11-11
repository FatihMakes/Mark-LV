#include "SandboxGameMode.h"
#include "HeroCharacterBase.h"
#include "HeroProfileSubsystem.h"
#include "WorldStateSubsystem.h"
#include "Kismet/GameplayStatics.h"

ASandboxGameMode::ASandboxGameMode()
{
	DefaultPawnClass = AHeroCharacterBase::StaticClass();
}

void ASandboxGameMode::BeginPlay()
{
	Super::BeginPlay();

	UGameInstance* GameInstance = GetGameInstance();
	if (!GameInstance)
	{
		return;
	}

	// Restore any persisted city destruction state for this session.
	if (UWorldStateSubsystem* WorldState = GameInstance->GetSubsystem<UWorldStateSubsystem>())
	{
		FString LoadError;
		WorldState->LoadWorldState(LoadError);
	}

	if (UHeroProfileSubsystem* ProfileSubsystem = GameInstance->GetSubsystem<UHeroProfileSubsystem>())
	{
		FString ProfileError;
		// Prefer the player's most recent preset (slot 0); otherwise bootstrap a species default.
		if (!ProfileSubsystem->LoadProfile(0, ProfileError))
		{
			ProfileSubsystem->InitializeDefaultProfile(DefaultSpecies, ProfileError);
		}
	}
}

void ASandboxGameMode::ApplyActiveProfileToPawn(AHeroCharacterBase* HeroPawn)
{
	if (!HeroPawn)
	{
		return;
	}

	UGameInstance* GameInstance = GetGameInstance();
	UHeroProfileSubsystem* ProfileSubsystem = GameInstance ? GameInstance->GetSubsystem<UHeroProfileSubsystem>() : nullptr;
	if (ProfileSubsystem)
	{
		HeroPawn->ApplyProfile(ProfileSubsystem->GetProfile());
	}
}
